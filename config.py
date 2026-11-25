"""muse2api 配置。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

_DEFAULT_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """自动加载 .env（patch22r：双路径 + 空值穿透）。

    ① data/runtime.env > base/.env——rotate key 经 _persist_env 写 runtime.env
      （在 data 卷内，容器重建不丢）；base/.env 在容器层，重启即失（22r 前的
      rotate 持久化是假的）。
    ② 环境变量为**空字符串**时视为未设置继续用 dotenv——compose 的
      `MUSE2API_KEY=${MUSE2API_KEY:-}` 会把空串注入 environ，原实现被空值
      压制后每次重启都重新生成 key 且轮换不落盘。
    不覆盖非空系统环境变量。"""
    base = os.environ.get("MUSE2API_HOME") or _DEFAULT_BASE_DIR
    candidates = [
        os.path.join(base, "data", "runtime.env"),
        os.path.join(base, ".env"),
    ]
    for env_path in candidates:
        if not os.path.isfile(env_path):
            continue
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and not os.environ.get(k):
                        os.environ[k] = v
        except OSError:
            pass


_load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _detect_chromium() -> str:
    """优先使用 MUSE2API_CHROMIUM；若未设置或路径不存在，则自动探测系统已安装的 Chromium/Chrome。"""
    configured = os.environ.get("MUSE2API_CHROMIUM", "").strip()
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured
    candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if os.path.isfile(c) or shutil.which(c):
            return c
    return configured or "chromium"


@dataclass
class Config:
    base_dir: str = field(default_factory=lambda: _env("MUSE2API_HOME", _DEFAULT_BASE_DIR))
    host: str = field(default_factory=lambda: _env("MUSE2API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("MUSE2API_PORT", "18610")))

    # 浏览器
    chromium: str = field(default_factory=_detect_chromium)
    cdp_port: int = field(default_factory=lambda: int(_env("MUSE2API_CDP_PORT", "19210")))
    home_dir: str = field(default_factory=lambda: _env("MUSE2API_HOME_DIR", os.path.expanduser("~")))
    extra_path: str = field(default_factory=lambda: _env("MUSE2API_EXTRA_PATH", "/snap/bin"))
    site_url: str = field(default_factory=lambda: _env("MUSE2API_SITE", "https://muse.ai/"))
    login_wait: int = field(default_factory=lambda: int(_env("MUSE2API_LOGIN_WAIT", "60")))

    # 浏览器 profile 根目录。
    # 踩过的坑：snap 版 chromium 受 AppArmor 约束，只能读写 $HOME 下的路径。
    # 传 --user-data-dir=/opt/... 或 /tmp/... 不会报错，但会被静默忽略，
    # 浏览器回落到 ~/snap/chromium/common/chromium。
    # 所以必须落在 $HOME 下（不要用点开头的隐藏目录，snap 的 home 接口排除隐藏文件）。
    profile_root: str = field(
        default_factory=lambda: _env("MUSE2API_PROFILE_ROOT",
                                     os.path.join(os.path.expanduser("~"),
                                                  "muse2api-profiles")))

    # 鉴权
    api_key: str = field(default_factory=lambda: _env("MUSE2API_KEY", ""))

    # 对外地址（管理页面显示「接入地址」用；留空则页面用当前域名兜底）
    public_base: str = field(
        default_factory=lambda: _env("MUSE2API_PUBLIC_BASE", ""))

    # 允许跨域调用导入接口的来源（Cookie 助手脚本从 muse.ai 页面提交时用）
    # patch22r：默认空=允许全部来源（["*"]，Web 客户端生态兼容——Open WebUI/
    # LobeChat 浏览器端直连）；要收紧 CORS 时显式设逗号分隔来源列表。
    cors_origins: str = field(
        default_factory=lambda: _env("MUSE2API_CORS_ORIGINS", ""))

    # 生成
    image_timeout: int = field(default_factory=lambda: int(_env("MUSE2API_IMAGE_TIMEOUT", "240")))
    video_timeout: int = field(default_factory=lambda: int(_env("MUSE2API_VIDEO_TIMEOUT", "600")))
    chat_timeout: int = field(default_factory=lambda: int(_env("MUSE2API_CHAT_TIMEOUT", "300")))
    max_concurrency: int = field(default_factory=lambda: max(1, int(_env("MUSE2API_CONCURRENCY", "8"))))
    # 二开（patch7）：任务历史自动修剪上限（写放大根修）；0=不限。
    tasks_max: int = field(default_factory=lambda: max(1, int(_env("MUSE2API_TASKS_MAX", "2000"))))
    # patch22o：已完成任务时间保留窗口（天）。0=只按条数裁。压测日 93 笔/天
    # 会把 120 条上限一天挤爆——2026-11-15 档案全灭导致 OpenPet 409 取证断链。
    tasks_keep_days: int = field(default_factory=lambda: max(0, int(_env("MUSE2API_TASKS_KEEP_DAYS", "30"))))
    # patch22r：归档索引条数上限（超限按 archived_at 淘最老）。0=不限。
    # 成品媒体本体有 mediabak 保留策略兜底，索引淘汰只影响溯源可见窗口。
    archive_index_max: int = field(default_factory=lambda: max(0, int(_env("MUSE2API_ARCHIVE_INDEX_MAX", "50000"))))
    # 二开（patch7）：媒体文件保留小时数；0=永久保留（现状默认）。
    media_retention_hours: int = field(default_factory=lambda: max(0, int(_env("MUSE2API_MEDIA_RETENTION_HOURS", "0"))))
    # 二开（patch8）：媒体云备份（MangoDrive/OpenList）。默认关闭；pass 建议宿主 .env 注入。
    backup_enabled: bool = field(default_factory=lambda: _env("MUSE2API_BACKUP_ENABLED", "0").strip() == "1")
    backup_app_user: str = field(default_factory=lambda: _env("MUSE2API_BACKUP_APP_USER", "app-muse2api"))
    backup_app_pass: str = field(default_factory=lambda: _env("MUSE2API_BACKUP_APP_PASS", ""))
    backup_base: str = field(default_factory=lambda: _env("MUSE2API_BACKUP_BASE", "/Apps/muse2api"))
    backup_mesh_url: str = field(default_factory=lambda: _env("MUSE2API_BACKUP_MESH_URL", "http://127.0.0.1:5244"))
    backup_public_url: str = field(default_factory=lambda: _env("MUSE2API_BACKUP_PUBLIC_URL", "https://api-drive.mangoqwq.com"))
    backup_interval: int = field(default_factory=lambda: max(60, int(_env("MUSE2API_BACKUP_INTERVAL", "300"))))
    backup_delete_local: bool = field(default_factory=lambda: _env("MUSE2API_BACKUP_DELETE_LOCAL", "1").strip() == "1")
    backup_max_age_hours: int = field(default_factory=lambda: max(0, int(_env("MUSE2API_BACKUP_MAX_AGE_HOURS", "0"))))

    # 参考图输入默认只允许调用方直接提供数据和公网 URL。
    # 仅可信单用户部署可显式设为 1 恢复服务器本地文件或私网抓取。
    ref_image_local_files: bool = field(
        default_factory=lambda: _env("MUSE2API_REF_IMAGE_LOCAL_FILES", "0").strip() == "1")
    ref_image_private_fetch: bool = field(
        default_factory=lambda: _env("MUSE2API_REF_IMAGE_PRIVATE_FETCH", "0").strip() == "1")

    # 工具调用（function calling）协议适配开关，**默认关闭**。
    # 实测 muse.ai 的助手会明确拒绝输出"伪工具调用"（原话：「我不会按这种 JSON
    # 格式输出'伪工具调用'」），强行注入协议反而会污染正常回答。
    # 所以默认只做「参数兼容」（客户端传 tools 不会 422），不注入、不解析。
    # 若目标模型行为有变，可设 MUSE2API_TOOL_PROTOCOL=1 打开这层适配再试。
    tool_protocol: bool = field(
        default_factory=lambda: _env("MUSE2API_TOOL_PROTOCOL", "0").strip() != "0")

    # 保活设置：是否对已禁用的账号执行自动保活，**默认关闭**。
    # 开启后，被管理员在后台手动禁用的账号仍会由后台守护进程执行自动保活与 VM 唤醒，
    # 保持会话活性不失效，但依然不会参与正常业务生图、生视频与对话轮转调度。
    keepalive_disabled_accounts: bool = field(
        default_factory=lambda: _env("MUSE2API_KEEPALIVE_DISABLED_ACCOUNTS", "0").strip() != "0")

    # ---- 二开：TetherPool (IP-Pool) 出口池集成 ----
    # MUSE2API_TETHERPOOL=1 开启后：账号代理填 tetherpool://managed 即由出口池托管；
    # 启动心跳为托管账号 acquire 出口；生成链路代理异常自动重绑。
    tetherpool_enabled: bool = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL", "0").strip() == "1")
    tetherpool_url: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_URL", "http://127.0.0.1:9100"))
    tetherpool_gateway: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_GATEWAY", "http://127.0.0.1:9101"))
    # patch22r：控制面服务 token 与网关共享密码拆分——11-18 IP-Pool 改造后
    # 控制面 acquire 要 Bearer service token，网关 9101 要 GATEWAY_PASSWORD
    # （全局共享），两个值不再相同。
    tetherpool_password: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_PASSWORD", "tetherpool"))
    tetherpool_gateway_password: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_GATEWAY_PASSWORD", ""))
    tetherpool_tier: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_TIER", "standard"))
    tetherpool_policy: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_POLICY", "standard_first"))
    # patch29：CN 出口排除 CIDR 集（逗号分隔）——acquire 必带 avoid_cidr，
    # 池侧硬过滤；配合 region=cn 拒绝层（tetherpool.is_cn_region）双保险。
    tetherpool_avoid_cn_cidrs: str = field(
        default_factory=lambda: _env("MUSE2API_TETHERPOOL_AVOID_CN_CIDRS",
                                     "183.193.0.0/16"))

    # ---- patch31：常驻登录锚点保活（页面级拟真保活 + cookie 收割）----
    # 背景：纯 HTTP 续签（renew_session_http）只能延长活着的会话，救不活被
    # muse.ai 服务端吊销的会话（风控/异地登录）；页面级保活让账号保持
    # 「活人在线」画像——真实浏览器加载 muse.ai/thread/new（页面自己跑
    # /api/session + WS 握手），随后从页面收割最新 cookie 合并回账号池。
    # 出口与生成链路共用同一绑定（粘性，零新漂移）；与生成互斥走账号租约
    # （非阻塞试拿，忙则跳过本轮，绝不硬抢）。
    anchor_keepalive_enabled: bool = field(
        default_factory=lambda: _env("MUSE2API_ANCHOR_KEEPALIVE", "1").strip() != "0")
    # 页面级保活最小间隔（秒）：单账号两次页面活动之间的下限。keepalive loop
    # 每 900s 跑一轮，每轮最多为 1 个账号做页面保活（轮转最久未保活的）。
    anchor_keepalive_interval: int = field(
        default_factory=lambda: max(300, int(_env("MUSE2API_ANCHOR_INTERVAL", "3600"))))

    @property
    def data_dir(self) -> str:
        return os.path.join(self.base_dir, "data")

    @property
    def media_dir(self) -> str:
        return os.path.join(self.data_dir, "media")

    @property
    def download_dir(self) -> str:
        return os.path.join(self.data_dir, "downloads")

    @property
    def profile_dir(self) -> str:
        """生成用浏览器 profile。"""
        return os.path.join(self.profile_root, "generate")

    @property
    def accounts_file(self) -> str:
        return os.path.join(self.data_dir, "accounts.json")

    @property
    def tasks_file(self) -> str:
        return os.path.join(self.data_dir, "tasks.json")

    @property
    def archive_index_file(self) -> str:
        """patch22o：裁剪免疫的成品溯源索引（文件名→任务档案）。"""
        return os.path.join(self.data_dir, "archive_index.json")

    @property
    def idempotency_file(self) -> str:
        """patch22o：视频幂等索引落盘（重启后同 key 重放仍命中原任务）。"""
        return os.path.join(self.data_dir, "idempotency_videos.json")

    def ensure_dirs(self):
        for d in (self.base_dir, self.data_dir, self.media_dir, self.download_dir,
                  self.profile_dir):
            os.makedirs(d, exist_ok=True)


CFG = Config()
