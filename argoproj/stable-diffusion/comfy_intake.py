"""Waiting page and switch request for ComfyUI (BOXP-498).

Runs beside Envoy while ComfyUI is stopped. It holds no credential and never
talks to ComfyUI: Envoy sends a request here whenever the shared phase says the
ComfyUI window is not open, or the request must not reach the backend.
"""
import json
import os
from pathlib import Path
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path('/comfy-switch')
RETRY = 30
PHASES = {
    'llm-open': 'LLM稼働中（動画生成は停止中）',
    'busy': '画像生成(SD)側が GPU を使用中',
    'fencing': 'LLM の受付を閉じています',
    'draining': 'LLM の処理完了を待っています',
    'stopping-llm': 'LLM を停止しています',
    'starting-sd': 'ComfyUI を起動しています',
    'image-ready': 'ComfyUI 利用可能',
    'image-draining': '実行中のジョブ完了を待って終了します（新しい投入は不可）',
    'stopping-sd': 'ComfyUI を停止しています',
    'starting-llm': 'LLM を起動しています',
    'verifying': 'LLM の応答を確認しています',
    'reopening': 'LLM の受付を再開しています',
    'failed': '切替に失敗しました',
}


def atomic(path, data):
    temporary = path.parent / ('.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Intake:
    def __init__(self, root=ROOT, clock=time.time):
        self.root, self.clock = Path(root), clock

    def phase(self):
        try:
            phase = (self.root / 'phase').read_text().strip()
        except OSError:
            return 'unavailable'
        return phase if phase in PHASES else 'unavailable'

    def requested(self):
        return (self.root / 'intent.json').exists()

    def intent(self):
        """True when a request is now waiting for the switcher."""
        # Only an idle LLM phase can be turned into a window. Recording a request
        # in any other phase would start an unattended window much later.
        if self.phase() != 'llm-open':
            return self.requested()
        if not self.requested():
            atomic(self.root / 'intent.json', json.dumps({'created': self.clock(), 'id': uuid.uuid4().hex}).encode())
        return True

    def summary(self):
        phase = self.phase()
        return {'phase': phase, 'requested': self.requested(), 'window_active': phase in ('image-ready', 'image-draining')}


def page(intake):
    phase = intake.phase()
    if phase == 'failed':
        guidance = '手動復旧が必要です。管理者に連絡してください。'
    elif phase == 'unavailable':
        guidance = '切替機構の状態を読めません。しばらくしてから再読み込みしてください。'
    elif phase == 'llm-open' and not intake.requested():
        guidance = ('ボタンを押すと LLM を停止して ComfyUI を起動します。起動まで数分かかり、'
                    'その間と利用中は LLM が使えません。投入したジョブがすべて終わり、'
                    'その後しばらく操作がなければ自動で LLM に戻ります。')
    else:
        guidance = '切替中です。このページは30秒ごとに更新され、準備ができると ComfyUI に変わります。'
    button = ''
    if phase == 'llm-open' and not intake.requested():
        button = ('<button onclick="this.disabled=true;fetch(\'/__comfy/switch\',{method:\'POST\','
                  'headers:{\'X-Comfy-Switch\':\'1\'}}).then(()=>location.reload())">動画生成モードへ切替</button>')
    return ('<!doctype html><html lang="ja"><meta charset="utf-8"><title>ComfyUI の準備</title>'
            '<meta http-equiv="refresh" content="30"><h1>ComfyUI の準備</h1><p>状態: '
            + PHASES.get(phase, '不明') + ' (' + phase + ')</p><p>' + guidance + '</p>' + button + '</html>').encode()


def handler(intake):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body, content_type='application/json', retry=False):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            if retry:
                self.send_header('Retry-After', str(RETRY))
            self.end_headers()
            self.wfile.write(body)

        def refuse(self):
            self.respond(503, {'phase': intake.phase(), 'message': 'ComfyUI is not accepting this request in the current phase.'}, retry=True)

        def do_POST(self):
            # Uploads and prompts are never stored here, so their bodies are not read.
            self.close_connection = True
            if self.path.split('?', 1)[0] != '/__comfy/switch':
                self.refuse()
                return
            # A custom header cannot be sent cross-site without a CORS preflight,
            # so another site cannot start a GPU switch.
            if self.headers.get('X-Comfy-Switch') != '1':
                self.respond(403, {'error': 'X-Comfy-Switch: 1 required'})
                return
            accepted = intake.intent()
            self.respond(202 if accepted else 409, intake.summary())

        def do_other(self):
            self.close_connection = True
            self.refuse()

        do_PUT = do_DELETE = do_PATCH = do_other

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in ('/__comfy/status', '/__comfy/health'):
                self.respond(200, intake.summary())
            elif path == '/' or 'text/html' in self.headers.get('Accept', ''):
                self.respond(503, page(intake), 'text/html; charset=utf-8', retry=True)
            else:
                self.refuse()

        def log_message(self, *args):
            pass

    return Handler


def main():
    ThreadingHTTPServer(('127.0.0.1', 8090), handler(Intake())).serve_forever()


if __name__ == '__main__':
    main()
