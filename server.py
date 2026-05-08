from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SUBMISSIONS_FILE = DATA_DIR / "submissions.json"
ADMIN_PASSWORD = "topadmin"


class ProposalHandler(SimpleHTTPRequestHandler):
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

        submissions = self.load_submissions()
        now = datetime.now(timezone.utc).astimezone()
        next_id = max((int(item.get("id", 0)) for item in submissions), default=0) + 1
        submission = {
            "id": next_id,
            "submitted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "data": row,
        }
        submissions.append(submission)
        self.save_submissions(submissions)
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

        submissions = self.load_submissions()
        remaining = [item for item in submissions if item.get("id") != submission_id]

        if len(remaining) == len(submissions):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        self.save_submissions(remaining)
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
        self.send_json(submission, HTTPStatus.OK)

    def read_json_body(self) -> object:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

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


def main() -> None:
    server_address = ("localhost", 8000)
    httpd = ThreadingHTTPServer(server_address, ProposalHandler)
    print("TOP 아이디어 제안서 서버가 실행 중입니다.")
    print("브라우저에서 http://localhost:8000 을 열어주세요.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
