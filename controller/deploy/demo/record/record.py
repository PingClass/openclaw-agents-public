#!/usr/bin/env python3
"""流程 ① 備援錄影：用 Playwright 錄前端示範模式（?demo=1），同時叫起 fake_agents.py 推進流程。

由 record.sh 呼叫（它會先起好臨時 controller、設好 FLOW_CONTROLLER_DB_URL 與臨時 admin 帳號）。
兩次人工確認不由 fake_agents 代按（--manual-approvals），而是在畫面上：
先看確認者的收件匣，再用手機尺寸的框打開一次性確認頁、按下確認。

段落：標題卡 → 總覽 → 工作流詳情時間軸逐步前進 → 收件匣＋手機確認頁（派車）→ 時間軸 →
收件匣＋手機確認頁（報帳）→ 時間軸 → 流程完成 → 財會收件匣 → 總覽 → 結尾卡。
每段畫面下方有繁中說明條（注入在頁面上，不改前端程式）。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

HERE = Path(__file__).resolve().parent
IDENTITIES_FILE = HERE.parents[1] / "identities.json"
VIEWPORT = {"width": 1280, "height": 720}

STEP_CAPTIONS = {
    "resolve_parties": "controller 對照 Identity，找出這次的業務、主管與財會",
    "draft_vehicle": "admin-agent 查 ADM 的空車，擬好派車草稿",
    "confirm_vehicle": "等業務確認派車（人工確認 1／2）",
    "book_vehicle": "業務確認了 → admin-agent 代業務在 ADM 預約車輛",
    "await_trip": "等業務出車回來，回報取車與還車里程",
    "record_trip": "admin-agent 在 ADM 登記取車、還車與里程",
    "draft_claim": "finance-agent 依里程擬好報帳草稿",
    "approve_claim": "等業務主管核准報帳（人工確認 2／2）",
    "notify_finance": "主管核准了 → controller 通知財會入帳",
    "close_visit": "cs-agent 在 CRM 記下拜訪互動、完成待辦跟進",
}

# 說明條、標題卡的樣式與操作（注入到頁面，不改前端程式）
OVERLAY_JS = """
(() => {
  if (window.__demoOverlay) return;
  const css = `
  #demo-caption { position: fixed; left: 0; right: 0; bottom: 0; z-index: 2147483646;
    display: flex; align-items: center; gap: 18px; padding: 14px 28px;
    background: rgba(15, 23, 42, .92); color: #f8fafc;
    font: 600 24px/1.4 "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
    transition: opacity .4s; }
  #demo-caption .tag { flex: none; font-size: 15px; font-weight: 500; padding: 4px 10px; border-radius: 6px;
    background: #2563eb; color: #fff; }
  #demo-caption .note { margin-left: auto; flex: none; font-size: 13px; font-weight: 400; color: #94a3b8; }
  #demo-card { position: fixed; inset: 0; z-index: 2147483647; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 18px; text-align: center;
    background: linear-gradient(135deg, #0f172a, #1e3a8a); color: #f8fafc;
    font-family: "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif; transition: opacity .6s; }
  #demo-card h1 { margin: 0; font-size: 52px; font-weight: 700; }
  #demo-card p { margin: 0; font-size: 26px; color: #cbd5e1; max-width: 1000px; line-height: 1.5; }
  #demo-card small { font-size: 17px; color: #94a3b8; }
  body { padding-bottom: 80px; }
  .demo-spot { outline: 4px solid #f59e0b !important; outline-offset: 4px; border-radius: 6px;
    transition: outline-color .3s; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
  window.__demoOverlay = {
    caption(tag, text) {
      let bar = document.getElementById('demo-caption');
      if (!bar) {
        bar = document.createElement('div');
        bar.id = 'demo-caption';
        bar.innerHTML = '<span class="tag"></span><span class="text"></span><span class="note">示範錄影・全為假資料</span>';
        document.body.appendChild(bar);
      }
      bar.querySelector('.tag').textContent = tag;
      bar.querySelector('.text').textContent = text;
    },
    card(title, lines, small) {
      let card = document.getElementById('demo-card');
      if (!card) {
        card = document.createElement('div');
        card.id = 'demo-card';
        document.body.appendChild(card);
      }
      card.style.opacity = '1';
      card.innerHTML = '';
      const h = document.createElement('h1'); h.textContent = title; card.appendChild(h);
      for (const line of lines) { const p = document.createElement('p'); p.textContent = line; card.appendChild(p); }
      if (small) { const s = document.createElement('small'); s.textContent = small; card.appendChild(s); }
    },
    hideCard() {
      const card = document.getElementById('demo-card');
      if (card) { card.style.opacity = '0'; setTimeout(() => card.remove(), 700); }
    },
  };
})();
"""

# 手機框頁面：同源（用 page.route 攔截一個不存在的路徑），左邊說明、右邊 390x780 的 iframe
PHONE_HTML = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><title>手機確認頁</title>
<style>
  html, body { margin: 0; height: 100%; background: linear-gradient(135deg, #e2e8f0, #cbd5e1);
    font-family: "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif; }
  .wrap { display: flex; align-items: center; justify-content: center; gap: 64px; height: 100%; }
  .side { width: 520px; color: #0f172a; }
  .side .tag { display: inline-block; font-size: 16px; padding: 4px 12px; border-radius: 6px;
    background: #2563eb; color: #fff; margin-bottom: 14px; }
  .side h2 { font-size: 34px; margin: 0 0 14px; }
  .side p { font-size: 21px; line-height: 1.6; color: #334155; margin: 0 0 10px; }
  .side small { font-size: 14px; color: #64748b; }
  .phone { width: 390px; height: 690px; border: 12px solid #0f172a; border-radius: 40px;
    overflow: hidden; background: #fff; box-shadow: 0 20px 50px rgba(15, 23, 42, .35); }
  .phone iframe { width: 390px; height: 690px; border: 0; display: block; }
</style></head>
<body><div class="wrap">
  <div class="side">
    <span class="tag">__TAG__</span>
    <h2>__TITLE__</h2>
    __LINES__
    <small>手機尺寸 390 寬・示範錄影・全為假資料</small>
  </div>
  <div class="phone"><iframe id="screen" title="確認頁" src="__SRC__"></iframe></div>
</div></body></html>
"""


def phone_html(tag: str, title: str, lines: list[str], src: str) -> str:
    esc = html.escape
    # CSS 裡有大括號，不能用 str.format
    return (PHONE_HTML.replace("__TAG__", esc(tag)).replace("__TITLE__", esc(title))
            .replace("__SRC__", esc(src, quote=True))
            .replace("__LINES__", "".join(f"<p>{esc(line)}</p>" for line in lines)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--backend-dir", required=True)
    parser.add_argument("--pace", type=float, default=7, help="fake_agents 每個 agent 步驟停留秒數")
    parser.add_argument("--log", help="fake_agents 的輸出寫到這個檔案")
    parser.add_argument("--name", default="flow1-visit-demo", help="輸出檔名（不含副檔名）")
    return parser.parse_args()


class Recorder:
    def __init__(self, args: argparse.Namespace, context: BrowserContext, page: Page) -> None:
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.context = context
        self.page = page
        self.flow_id: str | None = None
        identities = json.loads(IDENTITIES_FILE.read_text(encoding="utf-8"))["identities"]
        self.hrm = {r["agent_id"]: r["hrm_employee_id"] for r in identities}
        self.names = {r["agent_id"]: r["display_name"] for r in identities}
        # 手機框頁面：同源路徑（攔截，不打到後端），內容在每次開啟前由 approval() 設定
        self.phone_page = ""
        context.route("**/__demo_phone__", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=self.phone_page))

    # ------------------------------------------------------------ 共用
    def api(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}/api{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        response = self.context.request.get(url)
        if not response.ok:
            raise RuntimeError(f"GET {path} → {response.status}: {response.text()[:200]}")
        return response.json()

    def goto(self, path: str, page: Page | None = None) -> None:
        page = page or self.page
        # 前端開著 SSE（/api/events），networkidle 永遠等不到；等 DOM 好了再給畫面一點時間抓資料
        page.goto(self.base + path, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        page.evaluate(OVERLAY_JS)

    def caption(self, tag: str, text: str) -> None:
        self.page.evaluate("([t, x]) => window.__demoOverlay.caption(t, x)", [tag, text])
        print(f"[record] {tag}｜{text}", flush=True)

    def hold(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def flow(self) -> dict:
        assert self.flow_id
        return self.api(f"/flows/{self.flow_id}")

    def wait_for(self, predicate, timeout: float = 120, what: str = "") -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = self.flow()
            if predicate(detail):
                return detail
            self.page.wait_for_timeout(400)
        raise TimeoutError(f"等太久：{what}")

    def scroll_to_current(self) -> None:
        self.page.evaluate("""() => {
          const el = document.querySelector('.tl-current') || [...document.querySelectorAll('.tl-item')].pop();
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }""")

    def spot(self, selector: str, on: bool = True) -> None:
        self.page.evaluate(
            "([s, on]) => document.querySelectorAll(s).forEach((el) => el.classList.toggle('demo-spot', on))",
            [selector, on],
        )

    # ------------------------------------------------------------ 段落
    def title_card(self) -> None:
        self.goto("/?demo=1")
        self.page.evaluate("""() => window.__demoOverlay.card('流程 ①：客戶拜訪 → 派車 → 出車 → 報帳',
          ['cs-agent、admin-agent、finance-agent 三隻助理 agent 接力，Workflow Controller 串起整條流程',
           '兩次人工確認：業務確認派車、業務主管核准報帳'],
          '課堂備援錄影・假派工（fake agents）・畫面上全為假資料')""")
        self.hold(7)
        self.page.evaluate("() => window.__demoOverlay.hideCard()")
        self.hold(1)

    def overview_before(self) -> None:
        self.caption("總覽", "示範模式：字放大、只留總覽與時間軸，方便投影")
        self.hold(5)
        self.caption("總覽", "五隻助理 agent 的狀態卡；目前沒有進行中的流程")
        self.page.mouse.wheel(0, 400)
        self.hold(4)
        self.page.mouse.wheel(0, -400)
        self.hold(1)

    def start_flow(self) -> subprocess.Popen:
        # 直接用 backend 的 .venv python（record.sh 已 uv sync）：停掉時 terminate／kill 才打得到真正的行程，
        # 經過 `uv run` 包一層的話 SIGKILL 不會轉送，會留下孤兒 fake_agents
        python = Path(self.args.backend_dir) / ".venv" / "bin" / "python"
        cmd = [str(python), "scripts/fake_agents.py",
               "--base-url", self.base, "--setup-local", "--flow", "visit", "--manual-approvals",
               "--pace", str(self.args.pace), "--timeout", "900"]
        env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV",)}
        log = open(self.args.log, "w") if self.args.log else subprocess.DEVNULL  # noqa: SIM115
        try:
            proc = subprocess.Popen(cmd, cwd=self.args.backend_dir, env=env, stdout=log, stderr=subprocess.STDOUT)
        finally:
            if log is not subprocess.DEVNULL:
                log.close()  # 子行程已持有自己的檔案描述元
        self.caption("啟動", "cs-agent 從 CRM 找到即將到來的客戶拜訪，啟動流程 ①")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            items = self.api("/flows", {"type": "visit"}).get("items", [])
            if items:
                self.flow_id = items[0]["id"]
                break
            if proc.poll() is not None:
                raise RuntimeError(f"fake_agents 提早結束（exit {proc.returncode}），看 {self.args.log}")
            self.page.wait_for_timeout(300)
        else:
            raise TimeoutError("fake_agents 沒有建立流程")
        self.hold(5)  # 總覽即時更新：看到一條進行中的流程
        return proc

    def follow_timeline(self, until_steps: tuple[str, ...], terminal: bool = False) -> dict:
        """停在工作流詳情，每換一步就更新說明條並捲到目前步驟，直到流程到達 until_steps（或結束）。"""
        if not self.page.url.split("?")[0].endswith(f"/flows/{self.flow_id}"):
            self.goto(f"/flows/{self.flow_id}")
        shown: str | None = None
        while True:
            detail = self.flow()
            name = detail["current_step"]
            if detail["status"] in ("succeeded", "failed", "expired", "cancelled"):
                if not terminal:
                    raise RuntimeError(f"流程提早結束：{detail['status']}")
                return detail
            step = next((s for s in detail["steps"] if s["name"] == name), None)
            if name != shown:
                shown = name
                self.caption("時間軸", STEP_CAPTIONS.get(name, name))
                self.page.wait_for_timeout(600)
                self.scroll_to_current()
            if name in until_steps and step and step["status"] == "waiting_approval":
                return detail
            self.page.wait_for_timeout(400)

    def approval(self, step_name: str, approver: str, label: str, lines: list[str], comment: str) -> None:
        # 時間軸停在「等待確認」，給觀眾看清楚
        self.hold(4)
        recipient = self.hrm[approver]
        who = self.names[approver]

        # 桌面版收件匣（admin 可切換看任一位確認者）
        self.goto(f"/inbox?recipient={recipient}")
        self.caption("收件匣", f"{who} 的收件匣收到確認通知（LINE 接上前，以收件匣代替）")
        self.page.wait_for_selector("[data-testid=approval-link]")
        self.spot(".inbox-item.unread")
        self.hold(6)

        # 找出確認連結（一次性 token）
        inbox = self.api("/inbox", {"recipient": recipient, "unread_only": "true"})
        item = next(i for i in inbox["items"]
                    if i["kind"] == "approval_request" and i["flow_id"] == self.flow_id and i.get("link"))
        parsed = urllib.parse.urlparse(item["link"])
        approval_path = parsed.path + ("?" + parsed.query if parsed.query else "")

        # 手機框裡開確認頁
        self.phone_page = phone_html(f"人工確認・{label}", f"{who} 用手機打開確認連結", lines, approval_path)
        self.page.goto(self.base + "/__demo_phone__", wait_until="domcontentloaded")
        frame = self.page.frame_locator("#screen")
        frame.locator("button[data-option=approve]").wait_for()
        self.hold(7)
        frame.locator("textarea").click()
        frame.locator("textarea").press_sequentially(comment, delay=180)
        self.hold(1.5)
        frame.locator("button[data-option=approve]").click()
        frame.get_by_text("已送出").wait_for()
        self.hold(4)
        self.wait_for(lambda d: d["current_step"] != step_name, 30, f"{step_name} 決定後往下走")

    def finish(self, detail: dict) -> None:
        self.goto(f"/flows/{self.flow_id}")
        self.caption("完成", "流程成功結束：10 個步驟全部完成")
        self.page.evaluate("() => window.scrollTo({ top: 0, behavior: 'smooth' })")
        self.hold(4)
        self.page.mouse.wheel(0, 900)
        self.hold(3)
        self.page.mouse.wheel(0, 1600)
        self.hold(3)

        self.goto(f"/inbox?recipient={self.hrm['luminous']}")
        self.caption("收件匣", f"財會（{self.names['luminous']}）收到入帳通知")
        self.spot(".inbox-item")
        self.hold(6)

        self.goto("/?demo=1")
        self.caption("總覽", "今日統計更新：完成 1 條流程，沒有卡住")
        self.hold(6)
        self.page.evaluate("""() => window.__demoOverlay.card('流程 ① 完成',
          ['agent 只做自己的那一步，跨步驟的狀態、時限與人工確認都由 controller 管',
           '課堂現場網路或 LINE 不穩時，以這段錄影代替現場示範'],
          '重錄：controller/deploy/demo/record/record.sh')""")
        self.hold(6)


def to_mp4(webm: Path, mp4: Path) -> bool:
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("[record] 找不到 ffmpeg，只輸出 webm", flush=True)
        return False
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-preset", "medium", "-crf", "23", "-movflags", "+faststart", str(mp4)],
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    video_tmp = out_dir / ".raw"
    shutil.rmtree(video_tmp, ignore_errors=True)
    proc: subprocess.Popen | None = None
    ok = False
    started = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=VIEWPORT, record_video_dir=str(video_tmp), record_video_size=VIEWPORT,
            locale="zh-TW", timezone_id="Asia/Taipei", device_scale_factor=1,
        )
        # 先用 API 登入（cookie 在同一個 context），影片裡不出現登入頁與密碼
        login = context.request.post(f"{args.base_url}/api/auth/login", data={
            "username": os.environ["DEMO_ADMIN_USER"], "password": os.environ["DEMO_ADMIN_PASSWORD"]})
        if not login.ok:
            raise SystemExit(f"登入失敗：{login.status}")
        page = context.new_page()
        rec = Recorder(args, context, page)
        try:
            rec.title_card()
            rec.overview_before()
            proc = rec.start_flow()
            rec.follow_timeline(("confirm_vehicle",))
            rec.approval("confirm_vehicle", "mjolmire-businessman", "派車（1／2）",
                         ["admin-agent 擬好的派車：車輛、時段、目的地都列在確認頁上。",
                          "按下「核准派車」後，admin-agent 才會真的去 ADM 預約。",
                          "連結一次性、免登入、有截止時間。"], "OK，就這台")
            rec.follow_timeline(("approve_claim",))
            rec.approval("approve_claim", "main", "報帳（2／2）",
                         ["finance-agent 依里程差擬好的報帳草稿。",
                          "業務主管核准後，controller 才通知財會入帳。"], "核准")
            detail = rec.follow_timeline((), terminal=True)
            if detail["status"] != "succeeded":
                raise RuntimeError(f"流程結束狀態不是 succeeded：{detail['status']}")
            rec.hold(3)
            rec.finish(detail)
            ok = True
        finally:
            video = page.video
            try:
                context.close()
                browser.close()
            except BaseException:  # noqa: BLE001  Ctrl-C 時 Chromium 可能已被關掉、或再收到一次 SIGINT；仍要往下收尾
                ok = False
            if proc is not None:
                if not ok:
                    proc.terminate()  # 錄影失敗：不用等 fake_agents 的 900 秒逾時
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if not ok:
                shutil.rmtree(video_tmp, ignore_errors=True)
        raw = Path(video.path()) if video else None

    # 先確認整段都成功，才放到輸出位置：失敗時不留下（或覆蓋成）看似正常的影片
    if proc is None or proc.returncode != 0:
        print(f"[record] fake_agents 結束碼 {proc and proc.returncode}，看 {args.log}；不輸出影片", file=sys.stderr)
        shutil.rmtree(video_tmp, ignore_errors=True)
        return 1
    if raw is None or not raw.exists():
        print("[record] 沒有產生影片", file=sys.stderr)
        return 1
    webm = out_dir / f"{args.name}.webm"
    mp4 = out_dir / f"{args.name}.mp4"
    mp4.unlink(missing_ok=True)  # 舊的 mp4 不能留著假裝是這次的
    raw.replace(webm)
    shutil.rmtree(video_tmp, ignore_errors=True)
    print(f"[record] webm：{webm}（錄影約 {time.monotonic() - started:.0f} 秒）", flush=True)
    if to_mp4(webm, mp4):
        print(f"[record] mp4：{mp4}", flush=True)
    else:
        mp4.unlink(missing_ok=True)
        print("[record] 沒有輸出 mp4（ffmpeg 不在或轉檔失敗），只有 webm", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
