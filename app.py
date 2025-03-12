from pymongo import MongoClient
import subprocess
import platform
from datetime import datetime, timedelta
from threading import Lock
import re
from flask import Flask, jsonify, render_template, request, send_file
import pandas as pd
from io import BytesIO
from flask_cors import CORS
from flask import Response

app = Flask(__name__)

CORS(app)
MONGO_URI = "mongodb://localhost:27017/"
client = MongoClient(MONGO_URI)
db = client["camera_db"]
collection = db["camera_status"]

status_lock = Lock()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get_cameras', methods=['GET'])
def get_cameras():
    cameras = list(collection.find({}, {"_id": 0}))
    return jsonify(cameras)

@app.route('/add_camera', methods=['POST'])
def add_camera():
    data = request.form
    name, make, model, ip = data["name"], data["make"], data["model"], data["ip"]
    
    if collection.find_one({"ip": ip}):
        return jsonify({"error": "Camera already exists"}), 400
    
    camera_data = {
        "name": name,
        "make": make,
        "model": model,
        "ip": ip,
        "status": "UNKNOWN",
        "last_checked": datetime.min,  # Initialize with a very old timestamp
        "signal_strength": 0  # Initialize signal strength
    }
    collection.insert_one(camera_data)
    return jsonify({"message": "Camera added successfully"})

@app.route('/delete_camera/<ip>', methods=['DELETE'])
def delete_camera(ip):
    collection.delete_one({"ip": ip})
    return jsonify({"message": "Camera deleted successfully"})

def check_camera(ip):
    try:
        # OS-specific ping command
        ping_cmd = ["ping", "-n", "1", ip] if platform.system() == "Windows" else ["ping", "-c", "1", ip]
        result = subprocess.run(ping_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        response_time = None
        if platform.system() == "Windows":
            match = re.search(r"time[<=](\d+)ms", result.stdout)
        else:
            match = re.search(r"time=(\d+\.?\d*) ms", result.stdout)
        
        if match:
            response_time = float(match.group(1))  
        else:
            if "time<1ms" in result.stdout:
                response_time = 0.5  
        if response_time is not None:
            if response_time < 20:
                signal_strength = 5
            elif response_time < 100:
                signal_strength = 4
            elif response_time < 200:
                signal_strength = 3
            elif response_time < 300:
                signal_strength = 2
            else:
                signal_strength = 1
        else:
            signal_strength = 0  # No response (camera is unreachable)

        # Determine status based on signal strength
        if signal_strength == 0:
            return "OFFLINE", 0
        elif "Reply from" in result.stdout or "bytes from" in result.stdout:
            return "ONLINE", signal_strength
        else:
            return "OFFLINE", 0
    except Exception as e:
        print(f"Error checking camera {ip}: {e}")
        return "ERROR", 0

@app.route('/check_cameras', methods=['GET'])
def check_cameras():
    cameras = list(collection.find({}, {"_id": 0}))
    updated_cameras = []
    
    with status_lock:
        for cam in cameras:
            now = datetime.now()
            last_checked = cam.get("last_checked", datetime.min)
            if now - last_checked > timedelta(seconds=5):
                new_status, signal_strength = check_camera(cam["ip"])
                if cam["status"] != new_status:  
                    collection.update_one({"ip": cam["ip"]}, {"$set": {"status": new_status, "signal_strength": signal_strength, "last_checked": now}})
                cam["status"] = new_status
                cam["signal_strength"] = signal_strength
                updated_cameras.append(cam)

    return jsonify(updated_cameras)

@app.route('/generate_report', methods=['GET'])
def generate_report():
    try:
        # Fetch all cameras from the database
        cameras = list(collection.find({}, {"_id": 0}))

        # Calculate summary data
        online_cameras = [cam for cam in cameras if cam["status"] == "ONLINE"]
        offline_cameras = [cam for cam in cameras if cam["status"] == "OFFLINE"]
        unknown_cameras = [cam for cam in cameras if cam["status"] == "UNKNOWN"]

        # Prepare data for Excel
        summary_data = {
            "Total Cameras": len(cameras),
            "Online Cameras": len(online_cameras),
            "Offline Cameras": len(offline_cameras),
            "Unknown Cameras": len(unknown_cameras),
        }

        detailed_data = []
        for cam in offline_cameras:
            detailed_data.append([cam["name"], cam["make"], cam["model"], cam["ip"], cam["status"]])

        # Create a DataFrame for the detailed report (offline cameras)
        detailed_df = pd.DataFrame(detailed_data, columns=["Name", "Make", "Model", "IP", "Status"])

        # Create an Excel file using Pandas
        with BytesIO() as output:
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                detailed_df.to_excel(writer, index=False, sheet_name="Offline Cameras")

                # Create a summary sheet
                summary_df = pd.DataFrame(list(summary_data.items()), columns=["Metric", "Value"])
                summary_df.to_excel(writer, index=False, sheet_name="Summary")

            output.seek(0)
            
            return send_file(output, as_attachment=True, download_name="camera_report.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    
    except Exception as e:
        print(f"Error generating report: {e}")
        return jsonify({"error": "Failed to generate the report."}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
