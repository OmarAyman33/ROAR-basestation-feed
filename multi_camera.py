import cv2
from flask import Flask, Response

app = Flask(__name__)

# Dictionary to hold video capture objects
cameras = {}

def get_camera(cam_id):
    if cam_id not in cameras:
        cameras[cam_id] = cv2.VideoCapture(cam_id)
        # Optional: Lower resolution to save Wi-Fi bandwidth
        cameras[cam_id].set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        cameras[cam_id].set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    return cameras[cam_id]

def generate_frames(cam_id):
    camera = get_camera(cam_id)
    while True:
        success, frame = camera.read()
        if not success:
            break
        
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# Dynamic route to access different camera IDs
@app.route('/feed/<int:cam_id>')
def video_feed(cam_id):
    return Response(generate_frames(cam_id), 
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    print("Starting multi-camera server on port 5000...")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
