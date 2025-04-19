from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, send_from_directory
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy
from queue import Queue
import threading
import time
import os
import signal
import requests
import paramiko
import wave
import json

UPLOAD_FOLDER = "/home/james/server/sounds"
ALLOWED_EXTENSIONS = {'wav'}
INTERCOM_USERNAME = "<USERNAME>"
INTERCOM_PASSWORD = "<PASSWORD>"

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///intercom.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'dev'
app.template_folder = "templates"
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


os.makedirs("templates", exist_ok=True)

db = SQLAlchemy(app)
command_queue = Queue()
stop_event = threading.Event()

# --- Models ---
class Intercom(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    ip_address = db.Column(db.String(100))
    volume_modifier = db.Column(db.Integer, default=0)
    disabled = db.Column(db.Boolean, default=False)

class IntercomGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    intercoms = db.relationship('Intercom', secondary='group_membership')

class GroupMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    intercom_id = db.Column(db.Integer, db.ForeignKey('intercom.id'))
    intercom_group_id = db.Column(db.Integer, db.ForeignKey('intercom_group.id'))

class Sound(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    filename = db.Column(db.String(200))
    play_duration_ms = db.Column(db.Integer)
    volume_modifier = db.Column(db.Integer, default=0)

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    volume_modifier = db.Column(db.Integer, default=0)
    sound_order = db.Column(db.Text)  # Comma-separated list of sound IDs

class AnnouncementCommand(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    intercom_id = db.Column(db.Integer, db.ForeignKey('intercom.id'), nullable=True)
    intercom_group_id = db.Column(db.Integer, db.ForeignKey('intercom_group.id'), nullable=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcement.id'), nullable=True)
    sound_id = db.Column(db.Integer, db.ForeignKey('sound.id'), nullable=True)
    volume_modifier = db.Column(db.Integer, default=50)
    times_to_play = db.Column(db.Integer, default=1)
    loop_forever = db.Column(db.Boolean, default=False)

class SavedCommand(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    intercom_id = db.Column(db.Integer, db.ForeignKey('intercom.id'), nullable=True)
    intercom_group_id = db.Column(db.Integer, db.ForeignKey('intercom_group.id'), nullable=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcement.id'), nullable=True)
    sound_id = db.Column(db.Integer, db.ForeignKey('sound.id'), nullable=True)
    volume_modifier = db.Column(db.Integer, default=50)
    times_to_play = db.Column(db.Integer, default=1)
    loop_forever = db.Column(db.Boolean, default=False)

class SavedCommandSet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    commands = db.relationship("SavedCommand", secondary="saved_command_set_membership", backref="sets")

class SavedCommandSetMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    saved_command_id = db.Column(db.Integer, db.ForeignKey('saved_command.id'))
    set_id = db.Column(db.Integer, db.ForeignKey('saved_command_set.id'))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_wav_duration_ms(filepath):
    with wave.open(filepath, 'rb') as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        duration_seconds = frames / float(rate)
        return int(duration_seconds * 1000)  # convert to ms

# --- Queue Processor ---
def process_queue():
    import requests
    while not stop_event.is_set():
        try:
            cmd_id = command_queue.get(timeout=1)
        except:
            continue

        with app.app_context():
            cmd = db.session.query(AnnouncementCommand).get(cmd_id)
            if cmd is None:
                continue

            print(f"Processing command ID {cmd.id}...")
            targets = []

            if cmd.intercom_id:
                intercom = Intercom.query.get(cmd.intercom_id)
                if intercom:
                    targets.append(intercom)

            elif cmd.intercom_group_id:
                group = IntercomGroup.query.get(cmd.intercom_group_id)
                if group:
                    targets.extend(group.intercoms)

            if cmd.sound_id:
                sound = Sound.query.get(cmd.sound_id)
                for i in range(cmd.times_to_play):
                    start_time = int(time.time()) + 5
                    for intercom in targets:
                        if intercom.disabled:
                            continue

                        url = f"http://{intercom.ip_address}:8084/?type=sound&message={sound.filename}&times=1&volume={min(max(cmd.volume_modifier + intercom.volume_modifier + sound.volume_modifier, 5), 100)}&priority=100&id={cmd.id}&start_time={start_time}"
                        print(f"GET {url}")
                        try:
                            requests.get(url, timeout=1)
                        except Exception as e:
                            print(f"Failed to send to {intercom.name}: {e}")
                        time.sleep(0.1)
                    time.sleep(sound.play_duration_ms / 1000.0 + 2)
                time.sleep(2)

            elif cmd.announcement_id:
                announcement = Announcement.query.get(cmd.announcement_id)
                sound_ids = list(map(int, announcement.sound_order.split(',')))
                for i in range(cmd.times_to_play):
                    for sid in sound_ids:
                        sound = Sound.query.get(sid)
                        start_time = int(time.time()) + 5
                        for intercom in targets:
                            if intercom.disabled:
                                continue

                            volume = min(max(cmd.volume_modifier + intercom.volume_modifier + announcement.volume_modifier + sound.volume_modifier, 5), 100)
                            url = f"http://{intercom.ip_address}:8084/?type=sound&message={sound.filename}&times=1&volume={volume}&priority=100&id={cmd.id}&start_time={start_time}"
                            print(f"GET {url}")
                            try:
                                requests.get(url, timeout=1)
                            except Exception as e:
                                print(f"Failed to send to {intercom.name}: {e}")
                            time.sleep(0.1)
                        time.sleep(sound.play_duration_ms / 1000.0 + 2)
                    time.sleep(2)

            print(f"Finished processing command ID {cmd.id}")

            if cmd.loop_forever:
                command_queue.put(cmd.id)
            else:
                db.session.delete(cmd)
                db.session.commit()

        time.sleep(2)
        command_queue.task_done()


# --- Routes for remaining templates ---
@app.route("/intercoms")
def view_intercoms():
    intercoms = Intercom.query.all()
    return render_template("intercoms.html", intercoms=intercoms)

@app.route("/intercoms/add", methods=["GET", "POST"])
def add_intercom():
    if request.method == "POST":
        name = request.form["name"]
        ip = request.form["ip_address"]
        volume_modifier = int(request.form.get("volume_modifier", 0))
        intercom.disabled = "disabled" in request.form  # for both add and edit
        intercom = Intercom(name=name, ip_address=ip, volume_modifier=volume_modifier)
        db.session.add(intercom)
        db.session.commit()
        return redirect(url_for("view_intercoms"))
    return render_template("add_intercom.html")

@app.route("/groups")
def view_groups():
    groups = IntercomGroup.query.all()
    return render_template("groups.html", groups=groups)

@app.route("/commands/stopall")
def stop_all_playback():
    intercoms = Intercom.query.all()
    for intercom in intercoms:
        try:
            url = f"http://{intercom.ip_address}:8084/?type=cmd&cmd=stopall"
            print(f"Sending stop to: {url}")
            requests.get(url, timeout=1)
        except Exception as e:
            print(f"Failed to stop intercom {intercom.name}: {e}")
    flash("Stop command sent to all intercoms.")
    return redirect(url_for("view_commands"))

@app.route("/groups/add", methods=["GET", "POST"])
def add_group():
    intercoms = Intercom.query.all()
    if request.method == "POST":
        name = request.form["name"]
        intercom_ids = request.form.getlist("intercom_ids")
        group = IntercomGroup(name=name)
        db.session.add(group)
        db.session.commit()

        for intercom_id in intercom_ids:
            db.session.add(GroupMembership(intercom_id=intercom_id, intercom_group_id=group.id))
        db.session.commit()

        return redirect(url_for("view_groups"))
    return render_template("add_group.html", intercoms=intercoms)

@app.route("/groups/edit/<int:id>", methods=["GET", "POST"])
def edit_group(id):
    group = IntercomGroup.query.get_or_404(id)
    intercoms = Intercom.query.all()

    if request.method == "POST":
        group.name = request.form["name"]
        db.session.commit()

        # Clear existing memberships
        GroupMembership.query.filter_by(intercom_group_id=group.id).delete()

        # Add new memberships
        intercom_ids = request.form.getlist("intercom_ids")
        for intercom_id in intercom_ids:
            db.session.add(GroupMembership(intercom_id=intercom_id, intercom_group_id=group.id))
        db.session.commit()

        return redirect(url_for("view_groups"))

    return render_template("add_group.html", group=group, intercoms=intercoms)
@app.route("/intercoms/edit/<int:id>", methods=["GET", "POST"])
def edit_intercom(id):
    intercom = Intercom.query.get_or_404(id)
    if request.method == "POST":
        intercom.name = request.form["name"]
        intercom.ip_address = request.form["ip_address"]
        intercom.volume_modifier = int(request.form["volume_modifier"])
        intercom.disabled = "disabled" in request.form  # for both add and edit
        db.session.commit()
        return redirect(url_for("view_intercoms"))
    return render_template("add_intercom.html", intercom=intercom)


@app.route("/announcements")
def view_announcements():
    announcements = Announcement.query.all()
    sounds = {s.id: s.name for s in Sound.query.all()}
    return render_template("announcements.html", announcements=announcements, sounds=sounds)

@app.route("/announcements/add", methods=["GET", "POST"])
def add_announcement():
    if request.method == "POST":
        name = request.form["name"]
        volume_modifier = int(request.form.get("volume_modifier", 0))
        sound_order_ids = request.form.getlist("sound_order[]")
        sound_order = ",".join(sound_order_ids)  # Convert to comma-separated string

        ann = Announcement(
            name=name,
            volume_modifier=volume_modifier,
            sound_order=sound_order
        )
        db.session.add(ann)
        db.session.commit()
        return redirect(url_for("view_announcements"))

    sounds = Sound.query.all()
    return render_template("add_announcement.html", sounds=sounds)


@app.route("/commands/add", methods=["GET", "POST"])
def add_command():
    if request.method == "POST":
        intercom_id = request.form.get("intercom") or None
        group_id = request.form.get("group") or None
        announcement_id = request.form.get("announcement") or None
        sound_id = request.form.get("sound") or None
        volume = int(request.form.get("volume", 50))
        times = int(request.form.get("times", 1))
        loop = request.form.get("loop") == "on"

        cmd = AnnouncementCommand(
            intercom_id=intercom_id,
            intercom_group_id=group_id,
            announcement_id=announcement_id,
            sound_id=sound_id,
            volume_modifier=volume,
            times_to_play=times,
            loop_forever=loop
        )
        db.session.add(cmd)
        db.session.commit()
        command_queue.put(cmd.id)
        return redirect(url_for("view_commands"))

    intercoms = Intercom.query.all()
    groups = IntercomGroup.query.all()
    announcements = Announcement.query.all()
    sounds = Sound.query.all()
    return render_template("add_command.html", intercoms=intercoms, groups=groups, announcements=announcements, sounds=sounds)

@app.route("/sounds/add", methods=["GET", "POST"])
def add_sound():
    if request.method == "POST":
        name = request.form["name"]
        filename = request.form["filename"]
        play_duration_ms = int(request.form["play_duration_ms"])
        volume_modifier = int(request.form.get("volume_modifier", 0))

        sound = Sound(name=name, filename=filename, play_duration_ms=play_duration_ms, volume_modifier=volume_modifier)
        db.session.add(sound)
        db.session.commit()
        return redirect(url_for("view_sounds"))

    return render_template("add_sound.html")

@app.route("/commands/delete/<int:cmd_id>")
def delete_command(cmd_id):
    cmd = AnnouncementCommand.query.get(cmd_id)
    if cmd:
        db.session.delete(cmd)
        db.session.commit()
    return redirect(url_for("view_commands"))

@app.route("/commands")
def view_commands():
    commands = AnnouncementCommand.query.all()
    intercoms = {i.id: i.name for i in Intercom.query.all()}
    groups = {g.id: g.name for g in IntercomGroup.query.all()}
    announcements = {a.id: a.name for a in Announcement.query.all()}
    sounds = {s.id: s.name for s in Sound.query.all()}
    return render_template("commands.html", commands=commands, intercoms=intercoms, groups=groups, announcements=announcements, sounds=sounds)

@app.route("/sounds")
def view_sounds():
    sounds = Sound.query.all()
    return render_template("sounds.html", sounds=sounds)

@app.route("/saved_commands/edit/<int:id>", methods=["GET", "POST"])
def edit_saved_command(id):
    cmd = SavedCommand.query.get_or_404(id)
    if request.method == "POST":
        cmd.name = request.form["name"]
        cmd.intercom_id = request.form.get("intercom_id") or None
        cmd.intercom_group_id = request.form.get("intercom_group_id") or None
        cmd.announcement_id = request.form.get("announcement_id") or None
        cmd.sound_id = request.form.get("sound_id") or None
        cmd.volume_modifier = int(request.form.get("volume_modifier", 50))
        cmd.times_to_play = int(request.form.get("times_to_play", 1))
        cmd.loop_forever = bool(request.form.get("loop_forever"))
        db.session.commit()
        return redirect(url_for("view_saved_commands"))

    return render_template("edit_saved_command.html",
                           command=cmd,
                           intercoms=Intercom.query.all(),
                           groups=IntercomGroup.query.all(),
                           announcements=Announcement.query.all(),
                           sounds=Sound.query.all())

@app.route("/saved_commands/delete/<int:id>")
def delete_saved_command(id):
    cmd = SavedCommand.query.get_or_404(id)
    db.session.delete(cmd)
    db.session.commit()
    return redirect(url_for("view_saved_commands"))

@app.route("/saved_commands")
def view_saved_commands():
    commands = SavedCommand.query.all()
    return render_template("saved_commands.html", commands=commands)

@app.route("/saved_commands/add", methods=["GET", "POST"])
def add_saved_command():
    if request.method == "POST":
        name = request.form["name"]
        intercom_id = request.form.get("intercom_id") or None
        intercom_group_id = request.form.get("intercom_group_id") or None
        announcement_id = request.form.get("announcement_id") or None
        sound_id = request.form.get("sound_id") or None
        volume_modifier = int(request.form.get("volume_modifier", 50))
        times_to_play = int(request.form.get("times_to_play", 1))
        loop_forever = bool(request.form.get("loop_forever"))

        cmd = SavedCommand(
            name=name,
            intercom_id=intercom_id,
            intercom_group_id=intercom_group_id,
            announcement_id=announcement_id,
            sound_id=sound_id,
            volume_modifier=volume_modifier,
            times_to_play=times_to_play,
            loop_forever=loop_forever
        )
        db.session.add(cmd)
        db.session.commit()
        return redirect(url_for("view_saved_commands"))

    return render_template("add_saved_command.html",
                           intercoms=Intercom.query.all(),
                           groups=IntercomGroup.query.all(),
                           announcements=Announcement.query.all(),
                           sounds=Sound.query.all())

@app.route("/saved_commands/trigger/<int:id>")
def trigger_saved_command(id):
    saved = SavedCommand.query.get_or_404(id)
    new_cmd = AnnouncementCommand(
        intercom_id=saved.intercom_id,
        intercom_group_id=saved.intercom_group_id,
        announcement_id=saved.announcement_id,
        sound_id=saved.sound_id,
        volume_modifier=saved.volume_modifier,
        times_to_play=saved.times_to_play,
        loop_forever=saved.loop_forever
    )
    db.session.add(new_cmd)
    db.session.commit()
    command_queue.put(new_cmd.id)  # <--- This was missing
    return redirect(url_for("view_commands"))

@app.route("/commands/stopall_full")
def stop_all_and_clear():
    intercoms = Intercom.query.all()
    for intercom in intercoms:
        try:
            url = f"http://{intercom.ip_address}:8084/?type=cmd&cmd=stopall"
            print(f"Sending stop to: {url}")
            requests.get(url, timeout=1)
        except Exception as e:
            print(f"Failed to stop intercom {intercom.name}: {e}")

    # Clear the announcement command table
    AnnouncementCommand.query.delete()
    db.session.commit()

    # Clear the in-memory queue
    while not command_queue.empty():
        command_queue.get()
        command_queue.task_done()

    flash("Stop command sent and queue cleared.")
    return redirect(url_for("view_commands"))

@app.route("/sounds/upload", methods=["GET", "POST"])
def upload_sound():
    if request.method == "POST":
        file = request.files["file"]
        name = request.form["name"].strip()

        if file and file.filename.endswith(".wav"):
            filename = file.filename
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
            file.save(filepath)
            
            # Calculate duration using wave module
            duration = get_wav_duration_ms(filepath)

            # Save to database: name (user input), filename (original), duration
            sound = Sound(name=name, filename=os.path.splitext(filename)[0], play_duration_ms=duration)
            db.session.add(sound)
            db.session.commit()
            flash("Sound uploaded successfully.")
            return redirect(url_for("view_sounds"))

        flash("Invalid file. Please upload a .wav file.")
    return render_template("upload_sound.html")


@app.route("/announcements/edit/<int:id>", methods=["GET", "POST"])
def edit_announcement(id):
    ann = Announcement.query.get_or_404(id)
    if request.method == "POST":
        ann.name = request.form["name"]
        ann.volume_modifier = int(request.form.get("volume_modifier", 0))
        sound_order_ids = request.form.getlist("sound_order[]")
        ann.sound_order = ",".join(sound_order_ids)
        db.session.commit()
        return redirect(url_for("view_announcements"))

    sounds = Sound.query.all()
    return render_template("edit_announcement.html", announcement=ann, sounds=sounds)

@app.route("/announcements/delete/<int:id>")
def delete_announcement(id):
    ann = Announcement.query.get_or_404(id)
    db.session.delete(ann)
    db.session.commit()
    flash("Announcement deleted.")
    return redirect(url_for("view_announcements"))



@app.route("/groups/delete/<int:id>")
def delete_group(id):
    group = IntercomGroup.query.get_or_404(id)

    # Delete all related group memberships first
    GroupMembership.query.filter_by(intercom_group_id=group.id).delete()

    # Then delete the group itself
    db.session.delete(group)
    db.session.commit()
    flash("Group deleted successfully.")
    return redirect(url_for("view_groups"))

@app.route("/intercoms/delete/<int:id>")
def delete_intercom(id):
    intercom = Intercom.query.get_or_404(id)

    # Remove any group memberships first
    GroupMembership.query.filter_by(intercom_id=intercom.id).delete()

    # Then delete the intercom itself
    db.session.delete(intercom)
    db.session.commit()
    flash("Intercom deleted successfully.")
    return redirect(url_for("view_intercoms"))


@app.route("/sounds/sync")
def sync_sounds():

    intercoms = Intercom.query.all()
    for intercom in intercoms:
        try:
            ip = intercom.ip_address
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, username=INTERCOM_USERNAME, password=INTERCOM_PASSWORD, timeout=5)

            sftp = ssh.open_sftp()

            # Try to list existing remote files
            try:
                remote_files = {
                    attr.filename: attr.st_size
                    for attr in sftp.listdir_attr("/var/sounds")
                }
            except IOError:
                # If listing fails (e.g., folder doesn't exist), treat as empty
                remote_files = {}

            uploaded_count = 0
            for file in os.listdir(UPLOAD_FOLDER):
                if not file.endswith(".wav"):
                    continue

                local_path = os.path.join(UPLOAD_FOLDER, file)
                remote_path = f"/var/sounds/{file}"

                local_size = os.path.getsize(local_path)
                remote_size = remote_files.get(file)

                if remote_size == local_size:
                    continue  # File is already present with same size

                sftp.put(local_path, remote_path)
                uploaded_count += 1

            sftp.close()
            ssh.close()
            print(f"Synced to {intercom.name}: {uploaded_count} file(s) uploaded")

        except Exception as e:
            print(f"Sync failed for {intercom.name}: {e}")

    flash("Sound sync completed to all intercoms.")
    return redirect(url_for("view_sounds"))


@app.route("/intercoms/status")
def intercom_status():
    intercoms = Intercom.query.all()
    statuses = []

    for intercom in intercoms:
        ip = intercom.ip_address
        url = f"http://{ip}:8084/status"
        try:
            response = requests.get(url, timeout=1)
            result = response.text if response.ok else None
            statuses.append({
                "name": intercom.name,
                "ip": ip,
                "online": True,
                "response": result
            })
        except Exception:
            statuses.append({
                "name": intercom.name,
                "ip": ip,
                "online": False,
                "response": None
            })

    return render_template("intercom_status.html", statuses=statuses)


@app.route("/saved_command_sets")
def view_command_sets():
    sets = SavedCommandSet.query.all()
    return render_template("saved_command_sets.html", sets=sets)


@app.route("/saved_command_sets/add", methods=["GET", "POST"])
def add_command_set():
    if request.method == "POST":
        name = request.form["name"]
        command_ids = request.form.getlist("command_ids")

        set_obj = SavedCommandSet(name=name)
        db.session.add(set_obj)
        db.session.commit()

        for cmd_id in command_ids:
            db.session.add(SavedCommandSetMembership(set_id=set_obj.id, saved_command_id=cmd_id))
        db.session.commit()

        return redirect(url_for("view_command_sets"))

    commands = SavedCommand.query.all()
    return render_template("add_command_set.html", commands=commands)


@app.route("/saved_command_sets/edit/<int:id>", methods=["GET", "POST"])
def edit_command_set(id):
    set_obj = SavedCommandSet.query.get_or_404(id)
    if request.method == "POST":
        set_obj.name = request.form["name"]
        db.session.commit()

        # Clear existing set memberships
        SavedCommandSetMembership.query.filter_by(set_id=id).delete()
        command_ids = request.form.getlist("command_ids")
        for cmd_id in command_ids:
            db.session.add(SavedCommandSetMembership(set_id=id, saved_command_id=int(cmd_id)))
        db.session.commit()

        return redirect(url_for("view_command_sets"))

    commands = SavedCommand.query.all()
    selected_ids = {cmd.id for cmd in set_obj.commands}  # <- key fix here
    return render_template("edit_command_set.html", set=set_obj, commands=commands, selected_ids=selected_ids)

@app.route("/saved_command_sets/delete/<int:id>")
def delete_command_set(id):
    set_obj = SavedCommandSet.query.get_or_404(id)
    db.session.delete(set_obj)
    db.session.commit()
    return redirect(url_for("view_command_sets"))

@app.route("/saved_command_sets/trigger/<int:id>")
def trigger_command_set(id):
    set_obj = SavedCommandSet.query.get_or_404(id)
    for cmd in set_obj.commands:  # `cmd` is already a SavedCommand instance
        new_cmd = AnnouncementCommand(
            intercom_id=cmd.intercom_id,
            intercom_group_id=cmd.intercom_group_id,
            announcement_id=cmd.announcement_id,
            sound_id=cmd.sound_id,
            volume_modifier=cmd.volume_modifier,
            times_to_play=cmd.times_to_play,
            loop_forever=cmd.loop_forever
        )
        db.session.add(new_cmd)
        db.session.commit()
        command_queue.put(new_cmd.id)

    flash("Command set triggered.")
    return redirect(url_for("view_commands"))

@app.route("/")
def home():
    return render_template("home.html")


# --- Init ---
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        AnnouncementCommand.query.delete()
        db.session.commit()
        while not command_queue.empty():
            command_queue.get()
            command_queue.task_done()

    queue_thread = threading.Thread(target=process_queue)
    queue_thread.start()

    def shutdown_handler(signum, frame):
        print("Shutting down...")
        stop_event.set()
        queue_thread.join()
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    app.run(port=8000, host="0.0.0.0")
