from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import subprocess
import tempfile
import os
import uuid
import re

# Auto-download FFmpeg binary (no apt-get needed)
import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

app = Flask(__name__)
CORS(app)

MAX_DURATION = 5.0  # seconds
UPLOAD_FOLDER = tempfile.gettempdir()


def get_duration(filepath):
    """Get video duration using ffmpeg stderr output."""
    result = subprocess.run([
        FFMPEG, "-i", filepath
    ], capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if not match:
        raise ValueError("Could not determine video duration")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def process_reverse_edit(input_path, output_path):
    """
    Create the reverse edit effect:
    - Clip plays forward to midpoint
    - Then smoothly reverses back to start
    - Auto-scales to 720p max to save memory on free tier
    """
    duration = get_duration(input_path)
    mid = duration / 2

    uid = uuid.uuid4().hex
    tmp_forward = os.path.join(UPLOAD_FOLDER, f"fwd_{uid}.mp4")
    tmp_reverse = os.path.join(UPLOAD_FOLDER, f"rev_{uid}.mp4")

    # Scale filter: downscale to 720p max, keep aspect ratio, ensure even dimensions
    scale = "scale=1280:720:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"

    try:
        # Step 1: Extract first half (forward) scaled to 720p
        subprocess.run([
            FFMPEG, "-y",
            "-i", input_path,
            "-t", str(mid),
            "-vf", f"{scale},setpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-an",
            tmp_forward
        ], check=True, capture_output=True)

        # Step 2: Extract first half reversed, scaled to 720p
        subprocess.run([
            FFMPEG, "-y",
            "-i", input_path,
            "-t", str(mid),
            "-vf", f"reverse,{scale},setpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-an",
            tmp_reverse
        ], check=True, capture_output=True)

        # Step 3: Crossfade blend at midpoint
        fwd_dur = get_duration(tmp_forward)
        offset = max(0, fwd_dur - 0.08)

        subprocess.run([
            FFMPEG, "-y",
            "-i", tmp_forward,
            "-i", tmp_reverse,
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration=0.08:offset={offset:.4f}[outv]",
            "-map", "[outv]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path
        ], check=True, capture_output=True)

    finally:
        for f in [tmp_forward, tmp_reverse]:
            if os.path.exists(f):
                os.remove(f)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ReverseEdit API is running 🎬"})


@app.route("/process", methods=["POST"])
def process():
    if "clip" not in request.files:
        return jsonify({"error": "No clip uploaded"}), 400

    file = request.files["clip"]

    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    uid = uuid.uuid4().hex
    ext = os.path.splitext(file.filename)[1].lower() or ".mp4"
    input_path = os.path.join(UPLOAD_FOLDER, f"input_{uid}{ext}")
    output_path = os.path.join(UPLOAD_FOLDER, f"output_{uid}.mp4")

    try:
        file.save(input_path)

        # Check duration
        try:
            duration = get_duration(input_path)
        except Exception as e:
            return jsonify({"error": f"Could not read video file: {str(e)}"}), 400

        if duration > MAX_DURATION:
            return jsonify({
                "error": f"Clip is {duration:.1f}s — max is {MAX_DURATION}s. Trim it first!"
            }), 400

        if duration < 1.0:
            return jsonify({"error": "Clip is too short. Minimum is 1 second."}), 400

        # Process the reverse edit
        process_reverse_edit(input_path, output_path)

        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="reverseedit_output.mp4"
        )

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else "unknown"
        return jsonify({"error": f"FFmpeg failed: {stderr[-300:]}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if os.path.exists(input_path):
            os.remove(input_path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
