from platypush.context import get_backend
from platypush.message.response import Response

from platypush.plugins import Plugin


class CameraPiPlugin(Plugin):
    def start_recording(self):
        camera = get_backend('camera.pi')
        camera.send_camera_action(camera.CameraAction.START_RECORDING)
        return Response(output={'status':'ok'})

    def stop_recording(self):
        camera = get_backend('camera.pi')
        camera.send_camera_action(camera.CameraAction.STOP_RECORDING)
        return Response(output={'status':'ok'})

    def take_picture(self, image_file):
        camera = get_backend('camera.pi')
        camera.send_camera_action(camera.CameraAction.TAKE_PICTURE, image_file=image_file)
        return Response(output={'image_file':image_file})


# vim:sw=4:ts=4:et:
