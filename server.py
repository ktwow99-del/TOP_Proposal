from __future__ import annotations

import json
import re
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class UploadedFile:
    filename: str
    content: bytes


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SUBMISSIONS_FILE = DATA_DIR / "submissions.json"
ADMIN_PASSWORD = "topadmin"
HOST = "0.0.0.0"
PORT = 8000
SUBMISSIONS_LOCK = threading.Lock()


class ProposalHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Password")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/submissions":
            self.handle_get_submissions()
            return

        if path.startswith("/api/submissions/"):
            self.handle_get_submission(path)
            return

        if path == "/":
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/submissions":
            self.handle_create_submission()
            return

        if path.startswith("/api/submissions/") and path.endswith("/complete"):
            self.handle_complete_submission(path)
            return

        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path

        if path.startswith("/api/submissions/"):
            self.handle_delete_submission(path)
            return

        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def handle_create_submission(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        attachment = None

        if content_type.startswith("multipart/form-data"):
            payload, attachment = self.read_multipart_body()
        else:
            payload = self.read_json_body()

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            self.send_json({"error": "invalid payload"}, HTTPStatus.BAD_REQUEST)
            return

        row = payload["data"]
        required_fields = ["성명", "제안명", "과제 수행 계획"]
        missing_fields = [field for field in required_fields if not str(row.get(field, "")).strip()]

        if missing_fields:
            self.send_json(
                {"error": "missing required fields", "fields": missing_fields},
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with SUBMISSIONS_LOCK:
                submissions = self.load_submissions()
                now = datetime.now(timezone.utc).astimezone()
                next_id = max((int(item.get("id", 0)) for item in submissions), default=0) + 1
                submission = {
                    "id": next_id,
                    "submitted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "data": row,
                }

                if attachment is not None and attachment.filename:
                    saved_name = self.save_attachment(
                        attachment,
                        now.strftime("%Y-%m-%d"),
                        str(row.get("제안명", "")),
                    )
                    row["첨부파일"] = saved_name
                    submission["data"] = row

                submissions.append(submission)
                self.save_submissions(submissions)
        except OSError:
            self.send_json({"error": "save failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json(submission, HTTPStatus.CREATED)

    def handle_get_submissions(self) -> None:
        if self.headers.get("X-Admin-Password") != ADMIN_PASSWORD:
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        submissions = self.load_submissions()
        self.send_json(list(reversed(submissions)), HTTPStatus.OK)

    def handle_get_submission(self, path: str) -> None:
        if self.headers.get("X-Admin-Password") != ADMIN_PASSWORD:
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        submission_id_text = path.rsplit("/", 1)[-1]

        try:
            submission_id = int(submission_id_text)
        except ValueError:
            self.send_json({"error": "invalid id"}, HTTPStatus.BAD_REQUEST)
            return

        submissions = self.load_submissions()
        submission = next((item for item in submissions if item.get("id") == submission_id), None)

        if submission is None:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        self.send_json(submission, HTTPStatus.OK)

    def handle_delete_submission(self, path: str) -> None:
        if self.headers.get("X-Admin-Password") != ADMIN_PASSWORD:
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        submission_id_text = path.rsplit("/", 1)[-1]

        try:
            submission_id = int(submission_id_text)
        except ValueError:
            self.send_json({"error": "invalid id"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            with SUBMISSIONS_LOCK:
                submissions = self.load_submissions()
                remaining = [item for item in submissions if item.get("id") != submission_id]

                if len(remaining) == len(submissions):
                    self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return

                self.save_submissions(remaining)
        except OSError:
            self.send_json({"error": "save failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json({"deleted": submission_id}, HTTPStatus.OK)

    def handle_complete_submission(self, path: str) -> None:
        if self.headers.get("X-Admin-Password") != ADMIN_PASSWORD:
            self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        parts = path.strip("/").split("/")

        try:
            submission_id = int(parts[2])
        except (IndexError, ValueError):
            self.send_json({"error": "invalid id"}, HTTPStatus.BAD_REQUEST)
            return

        payload = self.read_json_body()

        try:
            with SUBMISSIONS_LOCK:
                submissions = self.load_submissions()
                submission = next((item for item in submissions if item.get("id") == submission_id), None)

                if submission is None:
                    self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return

                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    current_data = submission.get("data")
                    if not isinstance(current_data, dict):
                        current_data = {}

                    current_data.update(payload["data"])
                    submission["data"] = current_data

                now = datetime.now(timezone.utc).astimezone()
                submission["edit_completed"] = True
                submission["edit_completed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                self.save_submissions(submissions)
        except OSError:
            self.send_json({"error": "save failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json(submission, HTTPStatus.OK)

    def read_json_body(self) -> object:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def read_multipart_body(self) -> tuple[object, UploadedFile | None]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        fields = parse_multipart_form_data(self.headers.get("Content-Type", ""), raw_body)

        data_field = fields.get("data")
        attachment = fields.get("attachment")

        if not isinstance(attachment, UploadedFile) or not attachment.filename:
            attachment = None

        try:
            payload = json.loads(data_field) if isinstance(data_field, str) and data_field else None
        except json.JSONDecodeError:
            payload = None

        return payload, attachment

    @staticmethod
    def sanitize_filename_part(text: str, max_len: int = 80) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", str(text).strip())
        cleaned = cleaned.strip("._")
        return cleaned[:max_len] if cleaned else "unknown"

    def save_attachment(self, attachment: UploadedFile, receipt_date: str, task_name: str) -> str:
        original_name = Path(attachment.filename or "attachment").name
        date_part = self.sanitize_filename_part(receipt_date, 20)
        task_part = self.sanitize_filename_part(task_name)
        file_part = self.sanitize_filename_part(original_name)
        saved_name = f"{date_part}_{task_part}_{file_part}"

        DATA_DIR.mkdir(exist_ok=True)
        target = DATA_DIR / saved_name
        counter = 1

        while target.exists():
            stem = Path(saved_name).stem
            suffix = Path(saved_name).suffix
            target = DATA_DIR / f"{stem}_{counter}{suffix}"
            counter += 1

        with target.open("wb") as file:
            file.write(attachment.content)

        return target.name

    def load_submissions(self) -> list[dict]:
        if not SUBMISSIONS_FILE.exists():
            return []

        with SUBMISSIONS_FILE.open("r", encoding="utf-8") as file:
            try:
                data = json.load(file)
            except json.JSONDecodeError:
                return []

        return data if isinstance(data, list) else []

    def save_submissions(self, submissions: list[dict]) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        temp_file = SUBMISSIONS_FILE.with_suffix(".tmp")

        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(submissions, file, ensure_ascii=False, indent=2)

        temp_file.replace(SUBMISSIONS_FILE)

    def send_json(self, payload: object, status: HTTPStatus) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def get_multipart_boundary(content_type: str) -> str | None:
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary=") :].strip()
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
            return boundary
    return None


def parse_multipart_form_data(content_type: str, body: bytes) -> dict[str, str | UploadedFile]:
    boundary = get_multipart_boundary(content_type)
    if not boundary:
        return {}

    delimiter = f"--{boundary}".encode()
    fields: dict[str, str | UploadedFile] = {}

    for part in body.split(delimiter):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue

        header_body_split = part.split(b"\r\n\r\n", 1)
        if len(header_body_split) != 2:
            continue

        headers_raw, content = header_body_split
        if content.endswith(b"\r\n"):
            content = content[:-2]

        name = None
        filename = None
        for line in headers_raw.decode("utf-8", errors="replace").split("\r\n"):
            if not line.lower().startswith("content-disposition:"):
                continue

            name_match = re.search(r'name="([^"]*)"', line, re.IGNORECASE)
            filename_match = re.search(r'filename="([^"]*)"', line, re.IGNORECASE)
            if name_match:
                name = name_match.group(1)
            if filename_match:
                filename = filename_match.group(1)

        if not name:
            continue

        if filename:
            fields[name] = UploadedFile(filename=filename, content=content)
        else:
            fields[name] = content.decode("utf-8")

    return fields


def get_lan_ips() -> list[str]:
    ips: set[str] = set()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except socket.gaierror:
        pass

    return sorted(ips)


def main() -> None:
    server_address = (HOST, PORT)
    httpd = ThreadingHTTPServer(server_address, ProposalHandler)
    print("TOP 아이디어 제안서 서버가 실행 중입니다.")
    print(f"이 PC에서 접속: http://localhost:{PORT}")

    lan_ips = get_lan_ips()
    if lan_ips:
        print("사내망에서 접속:")
        for ip in lan_ips:
            print(f"  http://{ip}:{PORT}")
    else:
        print("사내망 주소 확인: ipconfig 명령에서 IPv4 주소를 확인한 뒤 http://IPv4주소:8000 으로 접속하세요.")

    httpd.serve_forever()


if __name__ == "__main__":
    main()
