"""Constants for TVCanvas."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "tvcanvas"

CONF_TOKEN: Final = "token"
CONF_TOKEN_EXPIRES_AT: Final = "token_expires_at"
CONF_CLOUD_URL: Final = "cloud_url"
CONF_ADVANCED: Final = "advanced"
CONF_CODE: Final = "code"

DEFAULT_CLOUD_URL: Final = "https://api.tvcanvas.app"

SYNC_INTERVAL: Final = timedelta(seconds=60)
MIN_INTERVAL_SECONDS: Final = 60
MAX_INTERVAL_SECONDS: Final = 86400
TOKEN_REFRESH_BEFORE: Final = timedelta(days=7)
# After a failed refresh: 15 minutes, then hourly (the cloud allows 5 an hour; #17).
TOKEN_REFRESH_RETRY: Final = (timedelta(minutes=15), timedelta(hours=1))
CREDITS_RETRY_SECONDS: Final = 1800
MAX_BACKOFF_SECONDS: Final = 900
PROBE_TIMEOUT_SECONDS: Final = 2

TV_WIDTH: Final = 3840
TV_HEIGHT: Final = 2160
JPEG_QUALITY: Final = 90

FETCH_TIMEOUT_SECONDS: Final = 15
MAX_IMAGE_BYTES: Final = 25 * 1024 * 1024  # direct fetches and cloud images
# Cloud JSON replies: the cloud's largest sync is 10 TVs × 64 KB of compact JSON (#17).
MAX_JSON_BYTES: Final = 4 * 1024 * 1024
MAX_TVS: Final = 20  # specs HA runs at once; the cloud allows 10 per HA
MAX_SOURCE_PIXELS: Final = 40_000_000  # fetched images (links, webcams, collections)
# Phone uploads: the phone sends ≤ 3840×2160 (8.3 MP, docs/lan-api.md §5); room for a
# 12 MP camera original (4032×3024). Decoding costs about 4 bytes a pixel (#16).
MAX_UPLOAD_PIXELS: Final = 16_000_000
# Decoding one image can take 150-380 MB: the whole integration decodes one at a time.
MAX_CONCURRENT_DECODES: Final = 1
MAX_REDIRECTS: Final = 3
MAX_ATTEMPTS_PER_TICK: Final = 5

FRAME_WS_PORT: Final = 8002
FRAME_CLIENT_NAME: Final = "TVCanvas-HA"
FRAME_PAIR_TIMEOUT_SECONDS: Final = 60
FRAME_DELETE_MAX_ATTEMPTS: Final = 3
FRAME_STALE_MAX: Final = 20
# Content ids HA uploaded to a Frame and has not deleted (kept with Replace previous off):
# ~40 bytes each in the store; a 180 s pin fills it in two days, and the oldest drop first.
FRAME_UPLOADED_MAX: Final = 1000
FRAME_MY_PHOTOS: Final = "MY-C0002"
FRAME_LOST_SLACK_SECONDS: Final = 5
# Following a TV to a new DHCP address (locate.py, locate_cast.py, follow.py).
LOCATE_EVERY_SECONDS: Final = 600
LOCATE_MAX_CANDIDATES: Final = 20
MDNS_BROWSE_SECONDS: Final = 5
FRAME_MDNS_TYPE: Final = "_samsungmsf._tcp.local."
FRAME_SSDP_TYPES: Final = (
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:samsung.com:device:RemoteControlReceiver:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
)
FRAME_INFO_PORT: Final = 8001
FRAME_INFO_TIMEOUT_SECONDS: Final = 5
FRAME_INFO_BODY_MAX: Final = 64 * 1024
# A Frame in standby (screen dark, still on the network) gets no renders or uploads; its
# PowerState is read from /api/v2/ before every tick, and every 30 s while it sleeps.
FRAME_POWER_TIMEOUT_SECONDS: Final = 3
FRAME_STANDBY_POLL_SECONDS: Final = 30

CAST_PORT: Final = 8009
# A Cast TV's identity: /setup/eureka_info (HTTPS, else plain HTTP on older devices).
CAST_MDNS_TYPE: Final = "_googlecast._tcp.local."
CAST_SSDP_TYPE: Final = "urn:dial-multiscreen-org:service:dial:1"
CAST_INFO_HTTPS_PORT: Final = 8443
CAST_INFO_HTTP_PORT: Final = 8008
CAST_INFO_PATH: Final = "/setup/eureka_info?params=device_info,name"
CAST_INFO_TIMEOUT_SECONDS: Final = 5
CAST_INFO_BODY_MAX: Final = 256 * 1024
CAST_LOAD_TIMEOUT_SECONDS: Final = 15
CAST_IMAGES_KEPT: Final = 3
IMAGE_VIEW_URL: Final = "/api/tvcanvas/image/{name}"

STORE_VERSION: Final = 1
SPEC_VERSION: Final = 1
STATUS_ERROR_MAX: Final = 200

# Photos from the phone over the LAN (docs/lan-api.md).
LAN_API_VERSION: Final = 1
LAN_KEY_HEADER: Final = "X-TVCanvas-LAN-Key"
LAN_CODE_TTL_SECONDS: Final = 600
LAN_MAX_WRONG_CODES: Final = 5
LAN_LOCKOUT_SECONDS: Final = 300
LAN_MAX_PHONES: Final = 5
LAN_DEVICE_NAME_MAX: Final = 64
LAN_PAIR_BODY_MAX: Final = 4 * 1024
LAN_IMAGE_BODY_MAX: Final = 10 * 1024 * 1024
LAN_PUTS_PER_MINUTE: Final = 120
LAN_UPLOADS_AT_ONCE: Final = 4  # read uploads waiting for (or in) the decode slot
LAN_UPLOADS_PER_PHONE: Final = 2  # PUTs one phone key may have in progress (it sends one)
LAN_BODY_READ_SECONDS: Final = 30  # a body (≤ 10 MB) not in by then gets 408
LAN_DECODE_WAIT_SECONDS: Final = 20  # the phone's PUT times out after 60 s
LAN_BUSY_RETRY_SECONDS: Final = 10
LAN_PROVES_PER_MINUTE: Final = 30
LAN_PROVE_BODY_MAX: Final = 4 * 1024
LAN_PROVE_CONTEXT: Final = "tvcanvas-lan-prove-v1:"
PHOTOS_DIR: Final = ("tvcanvas", "images")
PHOTOS_BYTES_CAP: Final = 1024 * 1024 * 1024
PHOTOS_FILES_CAP: Final = 400
PHOTOS_GC_AGE_SECONDS: Final = 600
