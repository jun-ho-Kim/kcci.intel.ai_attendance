# pip install python-dotenv
#pip install selenium
import logging as log
import sys
from argparse import ArgumentParser
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

import ctypes  # An included library with Python install.
sys.path.append(str(Path(__file__).resolve().parents[2] / 'common/python'))
sys.path.append(str(Path(__file__).resolve().parents[2] / 'common/python/openvino/model_zoo'))

from openvino.runtime import Core, get_version

from utils import crop
from landmarks_detector import LandmarksDetector
from face_detector import FaceDetector
from faces_database import FacesDatabase
from face_identifier import FaceIdentifier

import monitors

from helpers import resolution
from images_capture import open_images_capture

from model_api.models import OutputTransform
from model_api.performance_metrics import PerformanceMetrics
import selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException
import http.client as httplib
import socket

from selenium.webdriver.remote.command import Command

import time

from dotenv import load_dotenv
import os

import requests
load_dotenv()
id = os.environ.get("MY_ID")
pw = os.environ.get('MY_PW')
MY_NAME = "sihwan"

log.basicConfig(format='[ %(levelname)s ] %(message)s', level=log.DEBUG, stream=sys.stdout)

DEVICE_KINDS = ['CPU', 'GPU', 'HETERO']

ip_address = '10.10.14.3'
port = 8080
face_id = 0

def build_argparser():
    parser = ArgumentParser()

    general = parser.add_argument_group('General')
    #general.add_argument('-i', '--input', required=True,
    #                     help='Required. An input to process. The input must be a single image, '
    #                          'a folder of images, video file or camera id.')
    general.add_argument('--loop', default=False, action='store_true',
                         help='Optional. Enable reading the input in a loop.')
    general.add_argument('-o', '--output',
                         help='Optional. Name of the output file(s) to save.')
    general.add_argument('-limit', '--output_limit', default=1000, type=int,
                         help='Optional. Number of frames to store in output. '
                              'If 0 is set, all frames are stored.')
    general.add_argument('--output_resolution', default=None, type=resolution,
                         help='Optional. Specify the maximum output window resolution '
                              'in (width x height) format. Example: 1280x720. '
                              'Input frame size used by default.')
    general.add_argument('--no_show', action='store_true',
                         help="Optional. Don't show output.")
    general.add_argument('--crop_size', default=(0, 0), type=int, nargs=2,
                         help='Optional. Crop the input stream to this resolution.')
    general.add_argument('--match_algo', default='HUNGARIAN', choices=('HUNGARIAN', 'MIN_DIST'),
                         help='Optional. Algorithm for face matching. Default: HUNGARIAN.')
    general.add_argument('-u', '--utilization_monitors', default='', type=str,
                         help='Optional. List of monitors to show initially.')

    gallery = parser.add_argument_group('Faces database')
    gallery.add_argument('-fg', default='', help='Optional. Path to the face images directory.')
    gallery.add_argument('--run_detector', action='store_true',
                         help='Optional. Use Face Detection model to find faces '
                              'on the face images, otherwise use full images.')
    gallery.add_argument('--allow_grow', action='store_true',
                         help='Optional. Allow to grow faces gallery and to dump on disk. '
                              'Available only if --no_show option is off.')

    models = parser.add_argument_group('Models')
    models.add_argument('-m_fd', type=Path, required=False, default="./intel/face-detection-retail-0004/FP16/face-detection-retail-0004.xml",
                        help='Required. Path to an .xml file with Face Detection model.')
    models.add_argument('-m_lm', type=Path, required=False, default="./intel/landmarks-regression-retail-0009/FP16/landmarks-regression-retail-0009.xml",
                        help='Required. Path to an .xml file with Facial Landmarks Detection model.')
    models.add_argument('-m_reid', type=Path, required=False, default="./intel/face-reidentification-retail-0095/FP16/face-reidentification-retail-0095.xml",
                        help='Required. Path to an .xml file with Face Reidentification model.')
    models.add_argument('--fd_input_size', default=(0, 0), type=int, nargs=2,
                        help='Optional. Specify the input size of detection model for '
                             'reshaping. Example: 500 700.')

    infer = parser.add_argument_group('Inference options')
    infer.add_argument('-d_fd', default='CPU', choices=DEVICE_KINDS,
                       help='Optional. Target device for Face Detection model. '
                            'Default value is CPU.')
    infer.add_argument('-d_lm', default='CPU', choices=DEVICE_KINDS,
                       help='Optional. Target device for Facial Landmarks Detection '
                            'model. Default value is CPU.')
    infer.add_argument('-d_reid', default='CPU', choices=DEVICE_KINDS,
                       help='Optional. Target device for Face Reidentification '
                            'model. Default value is CPU.')
    infer.add_argument('-v', '--verbose', action='store_true',
                       help='Optional. Be more verbose.')
    infer.add_argument('-t_fd', metavar='[0..1]', type=float, default=0.6,
                       help='Optional. Probability threshold for face detections.')
    infer.add_argument('-t_id', metavar='[0..1]', type=float, default=0.3,
                       help='Optional. Cosine distance threshold between two vectors '
                            'for face identification.')
    infer.add_argument('-exp_r_fd', metavar='NUMBER', type=float, default=1.15,
                       help='Optional. Scaling ratio for bboxes passed to face recognition.')
    return parser


class FrameProcessor:
    QUEUE_SIZE = 16

    def __init__(self, args):
        self.allow_grow = args.allow_grow and not args.no_show

        log.info('OpenVINO Runtime')
        log.info('\tbuild: {}'.format(get_version()))
        core = Core()

        self.face_detector = FaceDetector(core, args.m_fd,
                                          args.fd_input_size,
                                          confidence_threshold=args.t_fd,
                                          roi_scale_factor=args.exp_r_fd)

        self.landmarks_detector = LandmarksDetector(core, args.m_lm)
        self.face_identifier = FaceIdentifier(core, args.m_reid,
                                              match_threshold=args.t_id,
                                              match_algo=args.match_algo)

        self.face_detector.deploy(args.d_fd)
        self.landmarks_detector.deploy(args.d_lm, self.QUEUE_SIZE)
        self.face_identifier.deploy(args.d_reid, self.QUEUE_SIZE)

        log.debug('Building faces database using images from {}'.format(args.fg))
        self.faces_database = FacesDatabase(args.fg, self.face_identifier,
                                            self.landmarks_detector,
                                            self.face_detector if args.run_detector else None, args.no_show)
        self.face_identifier.set_faces_database(self.faces_database)
        log.info('Database is built, registered {} identities'.format(len(self.faces_database)))

    def process(self, frame):
        orig_image = frame.copy()

        rois = self.face_detector.infer((frame,))
        if self.QUEUE_SIZE < len(rois):
            log.warning('Too many faces for processing. Will be processed only {} of {}'
                        .format(self.QUEUE_SIZE, len(rois)))
            rois = rois[:self.QUEUE_SIZE]

        landmarks = self.landmarks_detector.infer((frame, rois))
        face_identities, unknowns = self.face_identifier.infer((frame, rois, landmarks))
        if self.allow_grow and len(unknowns) > 0:
            for i in unknowns:
                # This check is preventing asking to save half-images in the boundary of images
                if rois[i].position[0] == 0.0 or rois[i].position[1] == 0.0 or \
                    (rois[i].position[0] + rois[i].size[0] > orig_image.shape[1]) or \
                    (rois[i].position[1] + rois[i].size[1] > orig_image.shape[0]):
                    continue
                crop_image = crop(orig_image, rois[i])
                name = self.faces_database.ask_to_save(crop_image)
                if name:
                    id = self.faces_database.dump_faces(crop_image, face_identities[i].descriptor, name)
                    face_identities[i].id = id

        return [rois, landmarks, face_identities]

def draw_detections(frame, frame_processor, detections, output_transform):
    now_user = ""
    size = frame.shape[:2]
    face_id = 0
    frame = output_transform.resize(frame)
    for roi, landmarks, identity in zip(*detections):
        face_id = identity.id

        text = frame_processor.face_identifier.get_identity_label(identity.id)
        now_user = text
        print("text is ", text)

        if identity.id != FaceIdentifier.UNKNOWN_ID:
            text += ' %.2f%%' % (100.0 * (1 - identity.distance))
        else :
            text = "Unregisted face"

        xmin = max(int(roi.position[0]), 0)
        ymin = max(int(roi.position[1]), 0)
        xmax = min(int(roi.position[0] + roi.size[0]), size[1])
        ymax = min(int(roi.position[1] + roi.size[1]), size[0])
        xmin, ymin, xmax, ymax = output_transform.scale([xmin, ymin, xmax, ymax])
        textsize = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 1)[0]
        if ((now_user == MY_NAME) &face_id > 0) :
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 220, 0), 2)
            cv2.rectangle(frame, (xmin+20, ymin-20), (xmin+20 + textsize[0], ymin - textsize[1]-20), (255, 255, 255), cv2.FILLED)
            cv2.putText(frame, text, (xmin+20, ymin-20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 1)            
        elif ((now_user != MY_NAME) & (face_id > 0)) :
            text = "You are not {}".format(MY_NAME)
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 102, 255), 5)
            cv2.rectangle(frame, (xmin, ymin-20), (xmax , ymin - textsize[1]-20), (255, 255, 255), cv2.FILLED)
            cv2.putText(frame, text, (xmin, ymin-20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 102, 255), 1)
        else :
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 0, 220), 7)
            cv2.rectangle(frame, (xmin+5, ymin-20), (xmin + textsize[0], ymin - textsize[1]-20), (0, 0, 255), cv2.FILLED)
            cv2.putText(frame, text, (xmin, ymin-20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 1)
        #cv2.circle(frame, (int((xmin+xmax)/2), int((ymin+ymax)/2)), 1, (0, 255, 255), 2)
        
        
    return [frame, face_id, now_user]

def center_crop(frame, crop_size):
    fh, fw, _ = frame.shape
    crop_size[0], crop_size[1] = min(fw, crop_size[0]), min(fh, crop_size[1])
    return frame[(fh - crop_size[1]) // 2 : (fh + crop_size[1]) // 2,
                 (fw - crop_size[0]) // 2 : (fw + crop_size[0]) // 2,
                 :]

def Mbox(title, text, style):
   return ctypes.windll.user32.MessageBoxW(0, text, title, style)

def main():
    face_id = 0
    args = build_argparser().parse_args()
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    w = 1920
    h = 1080    
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    _, frame = cap.read()
      
    frame_processor = FrameProcessor(args)

    frame_num = 0
    metrics = PerformanceMetrics()
    presenter = None
    output_transform = None
    input_crop = None
    if args.crop_size[0] > 0 and args.crop_size[1] > 0:
        input_crop = np.array(args.crop_size)
    elif not (args.crop_size[0] == 0 and args.crop_size[1] == 0):
        raise ValueError('Both crop height and width should be positive')
    video_writer = cv2.VideoWriter()
    
    while True:
        start_time = perf_counter()
        _, frame = cap.read()

        if frame is None:
            if frame_num == 0:
                raise ValueError("Can't read an image from the input")
            break
        if input_crop is not None:
            frame = center_crop(frame, input_crop)
        if frame_num == 0:
            output_transform = OutputTransform(frame.shape[:2], args.output_resolution)
            if args.output_resolution:
                output_resolution = output_transform.new_resolution
            else:
                output_resolution = (frame.shape[1], frame.shape[0])
            presenter = monitors.Presenter(args.utilization_monitors, 55,
                                           (round(output_resolution[0] / 4), round(output_resolution[1] / 8)))
            if args.output and not video_writer.open(args.output, cv2.VideoWriter_fourcc(*'MJPG'),
                                                     cap.fps(), output_resolution):
                raise RuntimeError("Can't open video writer")

        detections = frame_processor.process(frame)

        presenter.drawGraphs(frame)
        _, face_id, now_user = draw_detections(frame, frame_processor, detections, output_transform)
        metrics.update(start_time, frame)
        frame_num += 1
        if video_writer.isOpened() and (args.output_limit <= 0 or frame_num <= args.output_limit):
            video_writer.write(frame)

        if not args.no_show:
            cv2.imshow('Face recognition demo', frame)
            key = cv2.waitKey(1000)
            # Quit
            if((now_user == MY_NAME) & (face_id > 0)):
                key = 27
                ip = "http://{}:{}/num".format(ip_address, port)
                Mbox('Success', '인증이 성공되었습니다.', 1)
                cap.release()  # Release the camera
                cv2.destroyAllWindows()  # Close all OpenCV windows
                response = requests.get(ip)
                driver = webdriver.Chrome()

                driver.get("https://stclms.korchamhrd.net/local/ubattendance/autoattendance.php?id=154")

                time.sleep(1)  
                element = driver.find_element(By.ID, 'input-username')
                element.send_keys(id)
                element = driver.find_element(By.ID, 'input-password')
                element.send_keys(pw)
                element.send_keys("\n")

                time.sleep(1)
                element = driver.find_element(By.NAME, 'authkey')

                element.send_keys(response.text)
                while True:
                    try:
                        _ = driver.window_handles
                    except selenium.common.exceptions.InvalidSessionIdException as e:
                        break
                    time.sleep(1)
            elif ((now_user != MY_NAME) & (face_id > 0)):
                Mbox('False', '{}은 {}의 계정에 접속할 수 없습니다.'.format(now_user, MY_NAME), 1)
                cap.release()  # Release the camera
                cv2.destroyAllWindows()  # Close all OpenCV windows
            if key in {ord('q'), ord('Q'), 27}:
                break

            presenter.handleKey(key)
            

    metrics.log_total()
    for rep in presenter.reportMeans():

        log.info(rep)

if __name__ == '__main__':
    sys.exit(main() or 0)