import os, re, time, json, sqlite3, threading, subprocess, shutil
from pathlib import Path
from urllib.parse import quote

import requests
import pymysql
from flask import Flask, request, redirect, url_for, render_template_string, jsonify, flash, session, send_file, Response
from functools import wraps
import hmac
import difflib
from werkzeug.security import check_password_hash

APP_PORT = int(os.getenv('PORT', '3020'))
PROWLARR_URL = os.getenv('PROWLARR_URL', 'http://192.168.0.112:9696').rstrip('/')
PROWLARR_API_KEY = os.getenv('PROWLARR_API_KEY', '')
QBIT_URL = os.getenv('QBIT_URL', 'http://192.168.0.112:8181').rstrip('/')
QBIT_CATEGORY = os.getenv('QBIT_CATEGORY', 'roms')
QBIT_SAVE_PATH = os.getenv('QBIT_SAVE_PATH', '/media/ptw/roms/Downloaded ROMS/_incoming')
ROMS_ROOT = Path(os.getenv('ROMS_ROOT', '/roms'))
DOWNLOADED_ROOT = Path(os.getenv('DOWNLOADED_ROOT', '/roms/Downloaded ROMS'))
ROMM_RESOURCES_ROOT = Path(os.getenv('ROMM_RESOURCES_ROOT', '/romm_resources'))
DB_PATH = os.getenv('DB_PATH', '/data/gamefinder.db')
ROMM_DB_HOST = os.getenv('DB_HOST', '')
ROMM_DB_NAME = os.getenv('DB_NAME', 'romm')
ROMM_DB_USER = os.getenv('DB_USER', '')
ROMM_DB_PASSWORD = os.getenv('DB_PASSWD', '')
ROMM_URL = os.getenv('ROMM_URL', 'http://192.168.0.112:8382').rstrip('/')
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '45'))
PROWLARR_SEARCH_TIMEOUT = float(os.getenv('PROWLARR_SEARCH_TIMEOUT', '15'))
# Keep GameFinder scoped to fast console/ROM indexers instead of asking every Prowlarr
# indexer (movies/TV/general indexers make searches slow and return junk). Override with
# repeated Prowlarr indexer IDs if the server's Prowlarr IDs change.
def csv_values(name, default=''):
    return tuple(v.strip() for v in os.getenv(name, default).split(',') if v.strip())

GAMEFINDER_INDEXER_IDS = csv_values('GAMEFINDER_INDEXER_IDS', '17,23,25,19,6,5,1,7,12,16,22')
PROWLARR_CONSOLE_CATEGORIES = csv_values('PROWLARR_CONSOLE_CATEGORIES', '1000')
SEARCH_RESULT_LIMIT = int(os.getenv('GAMEFINDER_SEARCH_RESULT_LIMIT', '240'))
RESULTS_PER_PAGE = int(os.getenv('GAMEFINDER_RESULTS_PER_PAGE', '30'))
AUTH_USER = os.getenv('GAMEFINDER_USER', '')
AUTH_PASSWORD = os.getenv('GAMEFINDER_PASSWORD', '')
SHARED_AUTH_DB = os.getenv('SHARED_AUTH_DB', '/shared-auth/users.db')

SEARCH_SORT_OPTIONS = [
    ('relevance', 'Best match'),
    ('seeders', 'Most seeds'),
    ('peers', 'Most peers'),
    ('tracker', 'Tracker source'),
    ('platform', 'Console'),
    ('size', 'Largest size'),
]
SEARCH_SORT_OPTION_SET = {value for value, _label in SEARCH_SORT_OPTIONS}

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'gamefinder-local-secret')
app.config['SESSION_COOKIE_NAME'] = os.getenv('SESSION_COOKIE_NAME', 'gamefinder_session')

def authentik_username():
    username = (request.headers.get('X-authentik-username') or request.headers.get('X-Authentik-Username') or '').strip()
    email = (request.headers.get('X-authentik-email') or request.headers.get('X-Authentik-Email') or '').strip().lower()
    return username or (email.split('@', 1)[0] if email else '')

def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ak_user = authentik_username()
        if ak_user:
            session['logged_in'] = True
            session['authentik_user'] = ak_user
        if AUTH_USER and AUTH_PASSWORD and not session.get('logged_in'):
            return redirect(url_for('login', next=request.full_path if request.query_string else request.path))
        return fn(*args, **kwargs)
    return wrapper

def shared_auth_valid(username, password):
    username = (username or '').strip()
    if not username or not password or not SHARED_AUTH_DB or not Path(SHARED_AUTH_DB).exists():
        return False
    try:
        con = sqlite3.connect(f'file:{SHARED_AUTH_DB}?mode=ro', uri=True)
        row = con.execute('select password_hash from users where lower(username)=lower(?) and enabled=1', (username,)).fetchone()
        con.close()
        return bool(row and check_password_hash(row[0], password))
    except Exception as e:
        print('shared auth error', e, flush=True)
        return False

def login_valid(username, password):
    return (
        AUTH_USER and AUTH_PASSWORD
        and hmac.compare_digest(username or '', AUTH_USER)
        and hmac.compare_digest(password or '', AUTH_PASSWORD)
    ) or shared_auth_valid(username, password)

PLATFORM_ALIASES = [
    ('Xbox 360', ['xbox 360', '[xbox 360]', 'xbox360', '360']),
    ('Xbox', ['original xbox', '[xbox]', ' xbox ']),
    ('Xbox ONE', ['xbox one', 'xboxone']),
    ('Android', ['android', '[android]', '.apk', '.xapk', '.apkm']),
    ('Windows', ['windows', 'pc game', 'pc/games', 'steam', '.exe', '.msi']),
    ('Linux', ['linux', 'gnu/linux']),
    ('DOS', ['dos', 'ms-dos', 'dosbox']),
    ('nds', ['nds', '[nds]', 'nintendo ds', '.nds']),
    ('Nintendo DSi', ['dsi', 'nintendo dsi', '.dsi']),
    ('Nintendo GameBoy Advance', ['gba', 'gameboy advance', 'game boy advance', '.gba']),
    ('Nintendo GameBoy Color', ['gbc', 'gameboy color', 'game boy color', '.gbc']),
    ('Nintendo GameBoy', ['gameboy', 'game boy', '.gb']),
    ('Nintendo Entertainment System', ['nes', 'nintendo entertainment system', '.nes']),
    ('snes', ['snes', 'super nintendo', '.sfc', '.smc']),
    ('Nintendo 64', ['n64', 'nintendo 64', '.n64', '.z64', '.v64']),
    ('Nintendo Gamecube', ['gamecube', 'game cube', '.gcm']),
    ('Nintendo Wii', ['wii', '.wbfs', '.rvz']),
    ('Nintendo Wii U', ['wii u', 'wiiu']),
    ('Nintendo Switch', ['switch', 'nintendo switch']),
    ('PlayStation 1', ['playstation 1', 'ps1', 'psx']),
    ('PlayStation 2', ['playstation 2', 'ps2']),
    ('PlayStation Portable', ['psp', 'playstation portable', '.cso']),
    ('PlayStation 3', ['playstation 3', 'ps3']),
    ('PlayStation Vita', ['vita', 'playstation vita']),
    ('Sega Genisis', ['genesis', 'genisis', 'mega drive', '.gen', '.md']),
    ('Sega Dreamcast', ['dreamcast']),
    ('Sega Saturn', ['saturn']),
    ('Sega CD', ['sega cd', 'mega cd']),
    ('Sega - Game Gear', ['game gear', '.gg']),
    ('Sega - Master System - Mark III', ['master system', '.sms']),
    ('Atari 2600', ['atari 2600', '.a26']),
    ('Atari 5200', ['atari 5200', '.a52']),
    ('Atari 7800', ['atari 7800', '.a78']),
    ('Atari Jaguar', ['jaguar', '.j64']),
    ('Atari Lynx', ['lynx', '.lnx']),
    ('Neo Geo', ['neo geo', 'neogeo']),
]

def platform_options():
    """Return the actual RomM console folders available under /roms."""
    opts = [('', 'Auto-detect from title/result')]
    try:
        folders = sorted(
            p.name for p in ROMS_ROOT.iterdir()
            if p.is_dir() and p.name != 'Downloaded ROMS' and (p / 'roms').exists()
        )
        opts.extend((name, name) for name in folders)
    except Exception:
        opts.extend((name, name) for name, _aliases in PLATFORM_ALIASES)
    return opts


def normalize_sort_choice(value):
    value = (value or 'relevance').strip().lower()
    return value if value in SEARCH_SORT_OPTION_SET else 'relevance'


def sort_label(value):
    choice = normalize_sort_choice(value)
    for option_value, label in SEARCH_SORT_OPTIONS:
        if option_value == choice:
            return label
    return 'Best match'


def build_search_url(q, platform='', sort_by='relevance', page=1):
    params = [f"q={quote(q)}"]
    if platform:
        params.append(f"platform={quote(platform)}")
    sort_by = normalize_sort_choice(sort_by)
    if sort_by != 'relevance':
        params.append(f"sort={quote(sort_by)}")
    if page and page > 1:
        params.append(f"page={page}")
    return '/?' + '&'.join(params)

ROM_EXTS = {'.nes','.smc','.sfc','.gb','.gbc','.gba','.nds','.dsi','.n64','.z64','.v64','.iso','.cue','.bin','.chd','.cso','.rvz','.wbfs','.gcm','.xex','.gen','.md','.sms','.gg','.32x','.pce','.a26','.a52','.a78','.j64','.lnx','.ngp','.ngc','.ws','.wsc','.zip','.7z','.nsp','.nsz','.xci','.xcz','.apk','.xapk','.apkm'}
ARCHIVE_EXTS = {'.zip','.7z','.rar'}
EXECUTABLE_EXTS = {'.exe', '.msi', '.bat', '.cmd', '.com', '.sh', '.appimage'}
ANDROID_PAYLOAD_EXTS = {'.apk', '.xapk', '.apkm', '.obb'}

STYLE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="theme-color" content="#10091a"><style>
:root{color-scheme:dark;--bg:#10091a;--bg2:#0d0714;--card:rgba(38,26,56,.86);--card2:rgba(47,34,70,.92);--line:rgba(196,181,253,.16);--line2:rgba(196,181,253,.28);--text:#f5effb;--muted:#b6a8c7;--accent:#9f8ad7;--accent2:#c3a6e8;--royal:#8164b8;--deep:#6f55b5;--soft:#a78bfa;--dim:#7f6aa8}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(circle at top,#2b1b44 0,var(--bg) 46%,var(--bg2) 100%);color:var(--text);margin:0}a{color:var(--accent2)}.wrap{max-width:1280px;margin:0 auto;padding:24px}.card{background:linear-gradient(145deg,var(--card),rgba(24,15,37,.96));border:1px solid var(--line);border-radius:16px;padding:18px;margin:14px 0;box-shadow:0 14px 36px #0007}input,select,button{font:inherit;border-radius:10px;border:1px solid var(--line2);background:rgba(24,15,37,.9);color:var(--text);padding:10px}button{cursor:pointer;background:linear-gradient(135deg,var(--deep),var(--accent));border-color:rgba(196,181,253,.34);font-weight:750}.danger,.remove-btn{background:rgba(111,85,181,.22);border-color:rgba(195,166,232,.34)}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:minmax(0,1fr) 220px 120px;gap:10px}.result{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}.pill{display:inline-block;background:rgba(47,34,70,.82);border:1px solid var(--line);border-radius:999px;padding:3px 8px;margin:2px;font-size:12px}.ok,.bad{color:var(--accent2)}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}.small{font-size:12px}.top{display:flex;justify-content:space-between;gap:12px;align-items:center}.bar{height:10px;background:rgba(24,15,37,.9);border:1px solid var(--line2);border-radius:999px;overflow:hidden;min-width:160px}.bar>span{display:block;height:100%;background:linear-gradient(90deg,var(--deep),var(--accent2))}.status{font-weight:700}.nowrap{white-space:nowrap}.results-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:10px}.results-controls{display:flex;align-items:end;gap:10px;flex-wrap:wrap}.control{display:grid;gap:6px;min-width:180px}.control select{min-width:180px}
.jobs-shell{display:grid;gap:16px}.job-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.stat{background:rgba(24,15,37,.84);border:1px solid var(--line);border-radius:14px;padding:12px}.stat b{display:block;font-size:22px}.section-head{display:flex;justify-content:space-between;align-items:end;gap:12px;margin-top:10px}.job-list{display:grid;gap:12px}.job-card{background:rgba(24,15,37,.78);border:1px solid var(--line);border-radius:16px;padding:14px;display:grid;gap:12px}.job-card.active{border-color:rgba(159,138,215,.58);box-shadow:0 0 0 1px rgba(159,138,215,.26)}.job-card.complete{border-color:rgba(195,166,232,.36)}.job-top{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:start}.job-title{font-weight:750;font-size:16px;line-height:1.25;overflow-wrap:anywhere}.job-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.tag{display:inline-flex;align-items:center;gap:4px;background:rgba(47,34,70,.78);border:1px solid var(--line);border-radius:999px;padding:3px 8px;font-size:12px;color:#d8c8f3}.tag.good,.tag.warn,.tag.live{background:rgba(111,85,181,.24);border-color:rgba(195,166,232,.28);color:var(--accent2)}.job-grid{display:grid;grid-template-columns:1.1fr 1.1fr .75fr;gap:12px}.panel{background:rgba(24,15,37,.82);border:1px solid var(--line);border-radius:12px;padding:10px}.label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}.pathbox{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.35;overflow-wrap:anywhere;word-break:break-word;max-height:62px;overflow:auto}.msg{font-size:12px;color:#d8c8f3;line-height:1.35;max-height:50px;overflow:auto}.qbit-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}.qbit-card{background:rgba(24,15,37,.82);border:1px solid var(--line);border-radius:14px;padding:12px;display:grid;gap:8px}.qbit-name{font-weight:700;line-height:1.25;overflow-wrap:anywhere}.empty{text-align:center;color:var(--muted);padding:16px}.home-widgets{display:grid;gap:16px;margin-top:16px}.widget-head{display:flex;justify-content:space-between;align-items:end;gap:12px}.widget-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}.game-tile{display:grid;grid-template-rows:210px auto;background:rgba(24,15,37,.86);border:1px solid var(--line);border-radius:16px;overflow:hidden;text-decoration:none;color:var(--text);box-shadow:0 8px 20px #0005}.game-tile:hover{border-color:var(--accent);transform:translateY(-1px)}.game-cover{width:100%;height:210px;object-fit:cover;background:rgba(24,15,37,.9)}.game-info{padding:10px;display:grid;gap:6px}.game-name{font-weight:750;line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.game-platform{font-size:12px;color:var(--muted)}.game-rating{font-size:12px;color:var(--accent2)}.hero-strip{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.hero-panel{background:linear-gradient(135deg,#2b1b44,#180f25);border:1px solid var(--line);border-radius:18px;padding:18px;min-height:160px}.quick-picks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.pick{background:rgba(24,15,37,.86);border:1px solid var(--line);border-radius:14px;padding:12px;text-decoration:none;color:var(--text)}.pick b{display:block;margin-bottom:4px}@media(max-width:900px){.hero-strip{grid-template-columns:1fr}.widget-grid{grid-template-columns:repeat(auto-fill,minmax(135px,1fr))}.game-tile{grid-template-rows:180px auto}.game-cover{height:180px}.quick-picks{grid-template-columns:1fr}.job-grid,.job-summary{grid-template-columns:1fr}}@media(max-width:680px){body{font-size:16px}.wrap{padding:14px max(12px,env(safe-area-inset-left)) calc(22px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-right))}.top{position:sticky;top:0;z-index:4;margin:0 -14px 12px;padding:12px 14px;background:rgba(13,7,20,.92);backdrop-filter:blur(16px);align-items:flex-start;flex-direction:column}.top h1{font-size:28px;margin:0}.top a{display:inline-flex;padding:8px 4px}.card{border-radius:22px;padding:15px}.grid{grid-template-columns:1fr}.result{grid-template-columns:1fr}.result form button,.job-top form button{width:100%;min-height:46px}.results-controls,.control,.control select{width:100%;min-width:0}.job-top{grid-template-columns:1fr}.widget-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.game-tile{grid-template-rows:155px auto;border-radius:15px}.game-cover{height:155px}input,select,button{font-size:16px;min-height:46px;border-radius:14px}.qbit-list{grid-template-columns:1fr}.bar{min-width:0}}

/* Mobile/PWA hardening: no iOS input zoom, no sideways scroll, larger touch targets, phone-first grids. */
html,body{width:100%;max-width:100%;overflow-x:hidden;-webkit-text-size-adjust:100%}body{-webkit-font-smoothing:antialiased;touch-action:manipulation}.wrap{width:100%;max-width:1280px;overflow:hidden;padding:calc(16px + env(safe-area-inset-top)) calc(14px + env(safe-area-inset-right)) calc(24px + env(safe-area-inset-bottom)) calc(14px + env(safe-area-inset-left))}.top,.grid,.result,.card,.home-widgets,.hero-strip,.hero-panel,.widget-grid,.widget-head,.game-tile,.game-info,.job-card,.job-top,.job-grid,.panel,.qbit-card,.section-head,.results-head,.results-controls,form{min-width:0}.top{flex-wrap:wrap;align-items:flex-start}.top h1{font-size:clamp(28px,8vw,42px);line-height:1.05;margin:8px 0}.app-nav{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.top a,.pill,.tag{min-height:32px;display:inline-flex;align-items:center}.card{max-width:100%;overflow:hidden;border-radius:18px}.muted,.small,.job-title,.qbit-name,.game-name,.pathbox,.msg,.result b,.result span{overflow-wrap:anywhere}input,select,button,textarea{font-size:16px;line-height:1.2;min-height:44px;max-width:100%}input,select,textarea{width:100%;-webkit-appearance:none;appearance:none}button{touch-action:manipulation;-webkit-tap-highlight-color:transparent}.bar{max-width:100%;min-width:0}.result form button{width:100%}.widget-grid{grid-template-columns:repeat(auto-fit,minmax(138px,1fr))}.game-tile{min-width:0}.game-cover{max-width:100%;height:auto}.quick-picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.hero-strip,.job-grid{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}.job-summary{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}@media(max-width:720px){.wrap{padding-top:10px}.top{display:grid;gap:8px}.app-nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(92px,1fr));width:100%;gap:8px}.top a{text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:8px 10px;justify-content:center;color:var(--text)}.grid,.result,.job-top,.job-grid{grid-template-columns:1fr!important}.results-controls,.control,.control select{width:100%;min-width:0}.section-head,.results-head{display:grid;gap:10px}.card{padding:14px;margin:12px 0}.result{gap:10px}.result form{width:100%}.pill,.tag{white-space:normal}.nowrap{white-space:normal}.qbit-list{grid-template-columns:1fr}.pathbox,.msg{max-height:none}.home-widgets .card{padding:13px}.game-tile{display:grid;grid-template-columns:64px minmax(0,1fr);gap:10px;align-items:center}.game-cover{width:64px;height:88px;object-fit:cover;border-radius:10px}}

/* Clean iPhone shell: override older sticky/grid mobile rules with one compact normal-flow layout. */
@media(max-width:760px){
  html,body{overflow-x:hidden!important;background:#09050f!important}
  body{font-size:16px!important;background:radial-gradient(circle at 50% -8%,rgba(87,58,132,.48),transparent 40%),linear-gradient(180deg,#10091a 0%,#09050f 100%)!important}
  .wrap{width:100%!important;max-width:520px!important;margin:0 auto!important;padding:calc(12px + env(safe-area-inset-top)) 14px calc(96px + env(safe-area-inset-bottom))!important;overflow:visible!important}
  .top{position:relative!important;top:auto!important;z-index:auto!important;display:block!important;margin:0 0 12px!important;padding:0!important;background:transparent!important;backdrop-filter:none!important;box-shadow:none!important}
  .top h1{font-size:34px!important;line-height:1!important;letter-spacing:-.04em!important;margin:4px 0 12px!important;color:#fffaff!important}
  .app-nav{display:flex!important;flex-wrap:nowrap!important;gap:5px!important;overflow-x:auto!important;overscroll-behavior-x:contain!important;-webkit-overflow-scrolling:touch!important;width:calc(100% + 28px)!important;margin:0 -14px!important;padding:0 14px 8px!important;scrollbar-width:none!important}
  .app-nav::-webkit-scrollbar{display:none!important}
  .top a{flex:0 0 auto!important;display:inline-flex!important;align-items:center!important;justify-content:center!important;min-height:38px!important;height:38px!important;padding:0 8px!important;border:1px solid rgba(217,197,255,.22)!important;border-radius:999px!important;background:rgba(31,20,48,.82)!important;color:#f7f0ff!important;text-decoration:none!important;white-space:nowrap!important;font-size:14px!important;font-weight:750!important;box-shadow:0 8px 20px rgba(0,0,0,.22)!important}
  .card,.hero-panel,.job-card,.qbit-card,.panel,.stat{border-radius:22px!important;background:linear-gradient(145deg,rgba(34,23,50,.92),rgba(17,10,28,.96))!important;border-color:rgba(217,197,255,.14)!important;box-shadow:0 12px 30px rgba(0,0,0,.28)!important;padding:15px!important;margin:10px 0!important;overflow:hidden!important}
  .grid,.result,.job-top,.job-grid,.results-head,.section-head{display:grid!important;grid-template-columns:1fr!important;gap:10px!important}
  form.grid{gap:10px!important}
  input,select,button,textarea{width:100%!important;min-height:50px!important;font-size:16px!important;border-radius:16px!important;padding:12px 14px!important;background:rgba(12,7,19,.82)!important;border:1px solid rgba(217,197,255,.22)!important;color:#fffaff!important;box-shadow:none!important}
  select{padding-right:38px!important;background-image:linear-gradient(45deg,transparent 50%,#d9c5ff 50%),linear-gradient(135deg,#d9c5ff 50%,transparent 50%)!important;background-position:calc(100% - 22px) 21px,calc(100% - 15px) 21px!important;background-size:7px 7px,7px 7px!important;background-repeat:no-repeat!important}
  button{background:linear-gradient(135deg,#7356b6,#b196ee)!important;color:#fff!important;font-weight:850!important;border-color:rgba(231,214,255,.3)!important}
  p{line-height:1.48!important}.small{font-size:13px!important}.muted{color:#c7b8d8!important}.muted.small{line-height:1.45!important}
  .home-widgets{gap:12px!important;margin-top:12px!important}.hero-strip{display:grid!important;grid-template-columns:1fr!important;gap:10px!important}.quick-picks{grid-template-columns:1fr!important;gap:9px!important}.pick{border-radius:18px!important;padding:13px!important}
  .widget-head{display:grid!important;gap:4px!important}.widget-grid{grid-template-columns:1fr!important;gap:9px!important}.game-tile{display:grid!important;grid-template-columns:70px minmax(0,1fr)!important;grid-template-rows:auto!important;gap:11px!important;align-items:center!important;border-radius:18px!important;padding:9px!important}.game-cover{width:70px!important;height:92px!important;object-fit:cover!important;border-radius:13px!important}.game-info{padding:0!important}.game-name{-webkit-line-clamp:2!important}
  .results-controls,.control,.control select{width:100%!important;min-width:0!important}.pill,.tag{min-height:30px!important;white-space:normal!important}.bar{min-width:0!important}.pathbox,.msg{max-height:none!important}.nowrap{white-space:normal!important}
}
</style>
'''
TPL = STYLE + '''
<div class="wrap">
 <div class="top"><h1>GameFinder</h1><nav class="app-nav" aria-label="GameFinder navigation"><a href="https://info.personaltechwiz.com/">Home</a><a href="/">Search</a><a href="/jobs">Jobs</a><a href="{{ romm }}">RomM</a><a href="/admin/add-torrent">Add Torrent</a><a href="/logout">Logout</a></nav></div>
 {% with messages = get_flashed_messages() %}{% if messages %}<div class="card">{% for m in messages %}<div>{{m}}</div>{% endfor %}</div>{% endif %}{% endwith %}
 <div class="card">
  <form method="get" action="/" class="grid">
   <input name="q" value="{{q}}" placeholder="Search game name, e.g. Mario Kart DS">
   <select name="platform">{% for val,label in platforms %}<option value="{{val}}" {% if platform==val %}selected{% endif %}>{{label}}</option>{% endfor %}</select>
   <button>Search</button>
  </form>
  <p class="muted small">Searches your configured console/ROM Prowlarr indexers only. Add sends the selected result to qBittorrent category <b>{{cat}}</b> for seeding, then hardlinks/copies the finished payload into the matching console folder under <b>/roms/&lt;console&gt;/roms</b>. GameFinder does not search movie, TV, music, or general media categories and does not block duplicates already in your RomM collection.</p>
 </div>
 {% if results is none %}
 <div class="home-widgets">
  <div class="hero-strip">
   <div class="hero-panel">
    <h2 style="margin-top:0">Build the library faster</h2>
    <p class="muted">Use the widgets below to jump from what is already in RomM to more games worth adding. Every card opens a prefilled GameFinder search with the right platform.</p>
    <div class="quick-picks">
     <a class="pick" href="/?q=mario&platform=Nintendo+Wii"><b>Wii party picks</b><span class="muted small">Mario, Sonic, party games</span></a>
     <a class="pick" href="/?q=pokemon&platform=nds"><b>DS classics</b><span class="muted small">Easy handheld emulation</span></a>
     <a class="pick" href="/?q=zelda&platform=Nintendo+64"><b>N64 adventures</b><span class="muted small">Small files, high replay</span></a>
     <a class="pick" href="/?q=sonic&platform=Sega+Genisis"><b>Genesis speedrun shelf</b><span class="muted small">Fast downloads, easy emulation</span></a>
    </div>
   </div>
   <div class="hero-panel">
    <h2 style="margin-top:0">Library snapshot</h2>
    <p><b>{{landing.total_games}}</b> games indexed in RomM.</p>
    <p><b>{{landing.platform_count}}</b> platforms with files.</p>
    <p class="muted small">Thumbnails are served from the local RomM artwork cache.</p>
   </div>
  </div>
  {% for section in landing.sections %}
  <div class="card">
   <div class="widget-head"><div><h2 style="margin:0">{{section.title}}</h2><p class="muted small">{{section.subtitle}}</p></div></div>
   <div class="widget-grid">
    {% for g in section.games %}
    <a class="game-tile" href="/?q={{g.search_q}}&platform={{g.search_platform}}">
     <img class="game-cover" loading="lazy" src="{{g.thumb}}" alt="{{g.name}} cover">
     <div class="game-info">
      <div class="game-name">{{g.name}}</div>
      <div class="game-platform">{{g.platform}}</div>
      {% if g.rating %}<div class="game-rating">★ {{g.rating}}</div>{% endif %}
     </div>
    </a>
    {% endfor %}
    {% if not section.games %}<div class="empty">No games found for this widget yet.</div>{% endif %}
   </div>
  </div>
  {% endfor %}
 </div>
 {% endif %}
 {% if results is not none %}<div class="card"><div class="results-head"><div><h2 style="margin:0">Results for {{q}}</h2><div class="muted small">{{total_results}} live seeded result{% if total_results != 1 %}s{% endif %} · Sorted by {{sort_label}}</div></div><form method="get" action="/" class="results-controls"><input type="hidden" name="q" value="{{q}}"><input type="hidden" name="platform" value="{{platform}}"><label class="control"><span class="muted small">Sort results</span><select name="sort" onchange="this.form.submit()">{% for val,label in sort_options %}<option value="{{val}}" {% if sort_by==val %}selected{% endif %}>{{label}}</option>{% endfor %}</select></label><noscript><button type="submit">Apply</button></noscript></form></div>{% if not results %}<p>No live seeded results.</p>{% endif %}
  {% if total_pages and total_pages > 1 %}
  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 10px 0;">
    <div class="muted small">Showing page {{page}} / {{total_pages}}</div>
    <div style="display:flex;gap:8px;">
      {% if pager_prev %}<a href="{{pager_prev}}" style="padding:6px 10px;border:1px solid var(--line);border-radius:10px;color:var(--text);text-decoration:none">‹ Prev</a>{% endif %}
      {% if pager_next %}<a href="{{pager_next}}" style="padding:6px 10px;border:1px solid var(--line);border-radius:10px;color:var(--text);text-decoration:none">Next ›</a>{% endif %}
    </div>
  </div>
  {% endif %}
  {% for r in results %}<div class="result card">
    <div><b>{{r.title}}</b><br><span class="muted">{{r.indexer}} · {{r.size_h}} · {{r.protocol}} · Seeds {{r.seeders}} · Peers {{r.peers}}</span><br>
      {% if r.platform %}<span class="pill">{{r.platform}}</span>{% endif %}{% for c in r.categories %}<span class="pill">{{c}}</span>{% endfor %}</div>
    <form method="post" action="/add">
      <input type="hidden" name="title" value="{{r.title}}"><input type="hidden" name="url" value="{{r.url}}"><input type="hidden" name="platform" value="{{platform or r.platform}}"><input type="hidden" name="indexer" value="{{r.indexer}}"><input type="hidden" name="search_q" value="{{q}}"><input type="hidden" name="return_to" value="{{current_search_url}}"><button>Add to Library</button>
    </form>
  </div>{% endfor %}
  {% if total_pages and total_pages > 1 %}
  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0 0 0;">
  <div style="display:flex;gap:8px;">
    {% for p,url in pager_pages %}
      {% if p == page %}
        <span style="padding:6px 10px;border:1px solid var(--accent2);border-radius:10px;color:var(--accent2)">{{ p }}</span>
      {% else %}
        <a href="{{url}}" style="padding:6px 10px;border:1px solid var(--line);border-radius:10px;color:var(--text);text-decoration:none">{{ p }}</a>
      {% endif %}
    {% endfor %}
  </div>
  </div>
  {% endif %}
</div>{% endif %}
</div>'''
JOBS_TPL = STYLE + '''<meta http-equiv="refresh" content="10">
<div class="wrap">
 <div class="top"><h1>GameFinder Jobs</h1><nav class="app-nav" aria-label="GameFinder navigation"><a href="https://info.personaltechwiz.com/">Home</a><a href="/">Search</a><a href="{{ romm }}">RomM</a><a href="/logout">Logout</a></nav></div>
 {% with messages = get_flashed_messages() %}{% if messages %}<div class="card">{% for m in messages %}<div>{{m}}</div>{% endfor %}</div>{% endif %}{% endwith %}
 <div class="jobs-shell">
  <div class="card">
   <div class="section-head" style="margin-top:0"><div><h2 style="margin:0">Download pipeline</h2><p class="muted small">Auto-refreshes every 10 seconds. qBittorrent keeps the source in <b>_incoming</b> for seeding; GameFinder hardlinks/copies completed payloads into the real RomM console folder.</p></div></div>
   <div class="job-summary">
    <div class="stat"><span class="muted small">Active</span><b>{{active_jobs|length}}</b></div>
    <div class="stat"><span class="muted small">Completed</span><b>{{completed_jobs|length}}</b></div>
    <div class="stat"><span class="muted small">Live qBit ROMs</span><b>{{qbit_roms|length}}</b></div>
    <div class="stat"><span class="muted small">Total tracked</span><b>{{jobs|length}}</b></div>
   </div>
  </div>

  <div class="card">
   <div class="section-head"><div><h2 style="margin:0">Active downloads</h2><p class="muted small">Current torrent progress and RomM staging state.</p></div><span class="tag live">{{active_jobs|length}} active</span></div>
   <div class="job-list">
   {% if not active_jobs %}<div class="empty">No active GameFinder downloads.</div>{% endif %}
   {% for j in active_jobs %}
    <div class="job-card active">
     <div class="job-top">
      <div><div class="job-title">{{j.title}}</div><div class="job-meta"><span class="tag live">{{j.platform or 'Auto-detecting'}}</span><span class="tag">{{j.created_at}}</span><span class="tag">{{j.indexer}} · {{j.hash_short}}</span></div></div>
      <form method="post" action="/jobs/{{j.id}}/remove" onsubmit="return confirm('Remove this job? This will also remove the qBittorrent torrent when possible.');"><button class="remove-btn" type="submit">Remove</button></form>
     </div>
     <div class="job-grid">
      <div class="panel"><div class="label">Download</div><div class="status">{{j.download_label}}</div><div class="bar"><span style="width:{{j.progress_pct}}%"></span></div><div class="muted small">{{j.progress_pct}}% · {{j.qbit_state}}</div></div>
      <div class="panel"><div class="label">RomM stage</div><div class="status">{{j.status}}</div><div class="msg">{{j.message or 'Waiting for qBittorrent progress.'}}</div></div>
      <div class="panel"><div class="label">Speeds</div><div class="nowrap">↓ {{j.dlspeed}}</div><div class="nowrap">↑ {{j.upspeed}}</div></div>
     </div>
     <div class="panel"><div class="label">Current path</div><div class="pathbox">{{j.final_path or j.save_path or 'Path pending'}}</div></div>
    </div>
   {% endfor %}
   </div>
  </div>

  <div class="card">
   <div class="section-head"><div><h2 style="margin:0">Completed library adds</h2><p class="muted small">Already copied/hardlinked into RomM. Source torrents remain available for seeding.</p></div><span class="tag good">{{completed_jobs|length}} complete</span></div>
   <div class="job-list">
   {% if not completed_jobs %}<div class="empty">No completed GameFinder jobs yet.</div>{% endif %}
   {% for j in completed_jobs %}
    <div class="job-card complete">
     <div class="job-top">
      <div><div class="job-title">{{j.title}}</div><div class="job-meta"><span class="tag good">{{j.platform or 'Auto-detected'}}</span><span class="tag">{{j.created_at}}</span><span class="tag">{{j.indexer}} · {{j.hash_short}}</span><span class="tag">{{j.qbit_state}}</span></div></div>
      <form method="post" action="/jobs/{{j.id}}/remove" onsubmit="return confirm('Remove this job? This will also remove the qBittorrent torrent when possible.');"><button class="remove-btn" type="submit">Remove</button></form>
     </div>
     <div class="job-grid">
      <div class="panel"><div class="label">Download</div><div class="status ok">{{j.download_label}}</div><div class="bar"><span style="width:{{j.progress_pct}}%"></span></div><div class="muted small">{{j.progress_pct}}% · {{j.qbit_state}}</div></div>
      <div class="panel"><div class="label">RomM stage</div><div class="status ok">{{j.status}}</div><div class="msg">{{j.message or ''}}</div></div>
      <div class="panel"><div class="label">Speeds</div><div class="nowrap">↓ {{j.dlspeed}}</div><div class="nowrap">↑ {{j.upspeed}}</div></div>
     </div>
     <div class="panel"><div class="label">RomM path</div><div class="pathbox">{{j.final_path or j.save_path or 'Path pending'}}</div></div>
    </div>
   {% endfor %}
   </div>
  </div>

  <div class="card">
   <div class="section-head"><div><h2 style="margin:0">Live qBittorrent ROMs</h2><p class="muted small">Direct read from qBittorrent category <b>roms</b>; useful if GameFinder job rows and qBit disagree.</p></div></div>
   <div class="qbit-list">
   {% for r in qbit_roms %}
    <div class="qbit-card">
     <div class="qbit-name">{{r.name}}</div>
     <div class="job-meta"><span class="tag">{{r.hash_short}}</span><span class="tag {% if r.progress_pct >= 100 %}good{% else %}live{% endif %}">{{r.state}}</span><span class="tag">ETA {{r.eta}}</span></div>
     <div><div class="bar"><span style="width:{{r.progress_pct}}%"></span></div><div class="muted small">{{r.progress_pct}}% · Seeds {{r.seeds}} · Peers {{r.peers}}</div></div>
     <div class="muted small nowrap">↓ {{r.dlspeed}} · ↑ {{r.upspeed}}</div>
     <div class="pathbox">{{r.path}}</div>
    </div>
   {% endfor %}
   {% if not qbit_roms %}<div class="empty">No live ROM torrents in qBittorrent.</div>{% endif %}
   </div>
  </div>
 </div>
</div>'''
LOGIN_TPL = STYLE + '''<div class="wrap" style="max-width:480px"><h1>GameFinder Login</h1>{% with messages = get_flashed_messages() %}{% if messages %}<div class="card">{% for m in messages %}<div>{{m}}</div>{% endfor %}</div>{% endif %}{% endwith %}<div class="card"><form method="post"><input type="hidden" name="next" value="{{next}}"><p><label>Username</label><br><input name="username" autofocus style="width:100%"></p><p><label>Password</label><br><input name="password" type="password" style="width:100%"></p><button style="width:100%">Login</button></form></div></div>'''



LANDING_CACHE = {'at': 0, 'data': None}
EASY_PLATFORMS = ('Nintendo DS','nds','Nintendo GameBoy Advance','Nintendo GameBoy Color','Nintendo GameBoy','Nintendo Entertainment System','snes','Nintendo 64','Sega Genisis','Sega - Game Gear','Sega - Master System - Mark III','Atari 2600','Atari 5200','Atari 7800','PlayStation 1','PlayStation Portable')
CONSOLE_PICK_PLATFORMS = ('Xbox 360','Xbox','Xbox ONE','Nintendo Wii','Nintendo Wii U','Nintendo Gamecube','Nintendo Switch','PlayStation 2','PlayStation 3','PlayStation 4')

def romm_db_conn():
    if not (ROMM_DB_HOST and ROMM_DB_USER and ROMM_DB_PASSWORD):
        return None
    return pymysql.connect(host=ROMM_DB_HOST, user=ROMM_DB_USER, password=ROMM_DB_PASSWORD, database=ROMM_DB_NAME, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor, connect_timeout=3, read_timeout=8)

def clean_game_name(row):
    name = row.get('name') or row.get('fs_name_no_tags') or row.get('fs_name_no_ext') or row.get('fs_name') or 'Unknown game'
    return re.sub(r'\s+', ' ', name).strip()

def game_card(row):
    name = clean_game_name(row)
    platform = row.get('platform') or ''
    cover = (row.get('path_cover_s') or row.get('path_cover_l') or '').lstrip('/')
    rating = row.get('average_rating')
    try:
        rating = round(float(rating), 1) if rating is not None else None
    except Exception:
        rating = None
    return {'id': row.get('id'), 'name': name, 'platform': platform, 'rating': rating, 'thumb': url_for('romm_thumb', resource=cover) if cover else url_for('placeholder_cover', title=name), 'search_q': quote(name), 'search_platform': quote(platform)}

def romm_rows(where='', params=(), order='r.created_at desc', limit=8):
    con = romm_db_conn()
    if con is None:
        return []
    try:
        sql = '''
            select r.id, r.name, r.fs_name, r.fs_name_no_tags, r.fs_name_no_ext,
                   r.path_cover_s, r.path_cover_l, r.created_at, p.name as platform,
                   rm.average_rating, rm.genres
            from roms r
            join platforms p on p.id = r.platform_id
            left join roms_metadata rm on rm.rom_id = r.id
            where r.missing_from_fs = 0 {where}
            order by {order}
            limit %s
        '''.format(where=where, order=order)
        with con.cursor() as cur:
            cur.execute(sql, (*params, limit))
            return cur.fetchall()
    except Exception as e:
        print('romm widget query error', e, flush=True)
        return []
    finally:
        con.close()

def romm_counts():
    con = romm_db_conn()
    if con is None:
        return 0, 0
    try:
        with con.cursor() as cur:
            cur.execute('select count(*) games, count(distinct platform_id) platforms from roms where missing_from_fs=0')
            row = cur.fetchone() or {}
            return row.get('games', 0), row.get('platforms', 0)
    except Exception as e:
        print('romm count error', e, flush=True)
        return 0, 0
    finally:
        con.close()

def fallback_landing():
    con = db()
    rows = con.execute("select id, title as name, platform, final_path, created_at from jobs where status='complete' order by id desc limit 8").fetchall()
    con.close()
    games=[]
    for r in rows:
        row=dict(r); row['path_cover_s']=''; row['average_rating']=None; games.append(game_card(row))
    return {'total_games': len(games), 'platform_count': len({g['platform'] for g in games if g['platform']}), 'sections': [{'title':'Recently added games', 'subtitle':'Recent completed GameFinder jobs. RomM database unavailable.', 'games': games}]}

def landing_widgets():
    now = time.time()
    if LANDING_CACHE['data'] and now - LANDING_CACHE['at'] < 300:
        return LANDING_CACHE['data']
    total, platforms = romm_counts()
    if not total:
        data = fallback_landing()
    else:
        easy_placeholders = ','.join(['%s'] * len(EASY_PLATFORMS))
        console_placeholders = ','.join(['%s'] * len(CONSOLE_PICK_PLATFORMS))
        data = {'total_games': total, 'platform_count': platforms, 'sections': [
            {'title':'Recently added games', 'subtitle':'Fresh additions already indexed by RomM.', 'games':[game_card(r) for r in romm_rows('and (r.path_cover_s is not null or r.path_cover_l is not null)', order='r.created_at desc', limit=8)]},
            {'title':'Top games in your library', 'subtitle':'Highest-rated matches from RomM metadata.', 'games':[game_card(r) for r in romm_rows('and rm.average_rating is not null and (r.path_cover_s is not null or r.path_cover_l is not null)', order='rm.average_rating desc, r.created_at desc', limit=8)]},
            {'title':'Easy to emulate', 'subtitle':'Lightweight handheld and classic-console picks.', 'games':[game_card(r) for r in romm_rows('and p.name in (' + easy_placeholders + ') and (r.path_cover_s is not null or r.path_cover_l is not null)', EASY_PLATFORMS, order='coalesce(rm.average_rating,0) desc, r.created_at desc', limit=8)]},
            {'title':'Console picks', 'subtitle':'Bigger console games worth browsing for more releases.', 'games':[game_card(r) for r in romm_rows('and p.name in (' + console_placeholders + ') and (r.path_cover_s is not null or r.path_cover_l is not null)', CONSOLE_PICK_PLATFORMS, order='coalesce(rm.average_rating,0) desc, r.created_at desc', limit=8)]},
        ]}
    LANDING_CACHE.update({'at': now, 'data': data})
    return data

def placeholder_svg(title='Game'):
    safe = re.sub(r'[<>]', '', title or 'Game')[:42]
    return '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="440" viewBox="0 0 320 440"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#2b1b44"/><stop offset="1" stop-color="#10091a"/></linearGradient></defs><rect width="320" height="440" fill="url(#g)"/><rect x="22" y="22" width="276" height="396" rx="22" fill="none" stroke="#9f8ad7" stroke-width="3" opacity=".7"/><text x="160" y="205" text-anchor="middle" fill="#f5effb" font-family="Arial,sans-serif" font-size="28" font-weight="700">GameFinder</text><text x="160" y="250" text-anchor="middle" fill="#aab3d4" font-family="Arial,sans-serif" font-size="18">' + safe + '</text></svg>'

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('''create table if not exists jobs(id integer primary key, created_at text default current_timestamp, title text, platform text, indexer text, url text, hash text, status text, message text, final_path text)''')
    cols = {row[1] for row in con.execute('pragma table_info(jobs)').fetchall()}
    if 'imported_paths' not in cols:
        con.execute('alter table jobs add column imported_paths text')
        con.commit()
    return con

def human(n):
    try: n=int(n or 0)
    except: return ''
    for u in ['B','KB','MB','GB','TB']:
        if n<1024: return f"{n:.1f} {u}" if u!='B' else f"{n} B"
        n/=1024
    return f"{n:.1f} PB"

def speed(n):
    h = human(n)
    return (h + '/s') if h else '0 B/s'

def flatten_cats(cats):
    out=[]
    def walk(c):
        if c.get('name'): out.append(c['name'])
        for s in c.get('subCategories') or []: walk(s)
    for c in cats or []: walk(c)
    return out[:8]

def category_ids(cats):
    out=[]
    def walk(c):
        try:
            if c.get('id') is not None: out.append(int(c['id']))
        except Exception:
            pass
        for s in c.get('subCategories') or []: walk(s)
    for c in cats or []: walk(c)
    return out

PLATFORM_CATEGORY_IDS = {
    'nds': (1010,),
    'Nintendo DSi': (1010,),
    'PlayStation Portable': (1020,),
    'Nintendo Wii': (1030,),
    'Xbox': (1040,),
    'Xbox 360': (1050,),
    'PlayStation 3': (1080,),
    'Android': (4070,),
    'Nintendo 3DS': (1110,),
    'PlayStation Vita': (1120,),
    'Nintendo Wii U': (1130,),
    'Xbox ONE': (1140,),
    'PlayStation 4': (1180,),
}
PLATFORM_SEARCH_CATEGORY_IDS = PLATFORM_CATEGORY_IDS
CONSOLE_TITLE_TOKENS = ('nintendo','switch','nds','3ds','wii','gamecube','gba','gbc','game boy','gameboy','nes','snes','n64','playstation','ps1','ps2','ps3','ps4','psp','vita','xbox','dreamcast','saturn','sega','atari','rom')
BAD_CATEGORY_PREFIXES = ('movies', 'tv', 'audio', 'books', 'xxx')
BAD_RESULT_TITLE_TOKENS = ('cheat', 'trainer', 'save game', 'savegame', 'editor', 'walkthrough', 'mp4', 'mkv', 'avi', 'title update', 'dashboard update', 'theme', 'avatar pack')
SEARCH_STOPWORDS = {'the', 'and', 'for', 'with', 'edition', 'usa', 'eur', 'pal', 'ntsc', 'eng', 'multi', 'region', 'xbox', '360', 'nintendo', 'playstation', 'at'}
ROMAN_NUMERAL_TOKENS = {'i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii'}
NON_DISTINCTIVE_QUERY_TOKENS = {'grand', 'theft', 'auto', 'game', 'games'}
COLLECTION_RESULT_TOKENS = {'collection', 'pack', 'anthology', 'trilogy', 'bundle', 'compilation', 'core'}
SPINOFF_RESULT_TOKENS = {'help', 'wanted', 'secret', 'security', 'sister', 'world', 'into', 'mimic', 'breach', 'pit', 'simulator', 'custom', 'location'}
REPACK_RESULT_TOKENS = {'repack', 'portable', 'setup', 'exe'}
PC_RESULT_HINTS = ('windows', 'pc/games', 'pc game', '.exe', '.msi', 'gog', 'steam', 'portable')
CONSOLE_ONLY_HINTS = (' nsw ', ' switch ', ' ps1 ', ' ps2 ', ' ps3 ', ' ps4 ', ' ps5 ', ' psp ', ' vita ', ' xbox ', ' wii ', ' wii u ', ' nds ', ' 3ds ', ' quest ', ' meta quest ')

def normalize_search_text(text):
    s = (text or '').lower()
    s = s.replace('&amp;', ' and ')
    s = s.replace('&', ' and ')
    s = s.replace('&#039;', "'")
    s = s.replace('&apos;', "'")
    s = s.replace('’', "'")
    s = re.sub(r"\b([a-z0-9]+)'s\b", r'\1s', s)
    s = re.sub(r'\bgta\s*([1-5])\b', r'gta \1', s)
    s = re.sub(r'\bgta([1-5])\b', r'gta \1', s)
    s = re.sub(r'\bxbox360\b', 'xbox 360', s)
    s = re.sub(r'\bgta\b', 'grand theft auto', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    s = re.sub(r'\b([a-z0-9]+)\s+039\s+s\b', r'\1s', s)
    return re.sub(r'\s+', ' ', s).strip()


def token_fuzzy_match(token, text):
    if not token or not text:
        return False
    if f' {token} ' in f' {text} ':
        return True
    if len(token) <= 2:
        return False
    best = 0.0
    for w in text.split():
        if abs(len(w) - len(token)) > 2:
            continue
        ratio = difflib.SequenceMatcher(None, token, w).ratio()
        if ratio > best:
            best = ratio
    if len(token) >= 5:
        return best >= 0.78
    return best >= 0.9

def is_obvious_junk_title(title, query=''):
    hay = f" {normalize_search_text(title)} "
    query_n = f" {normalize_search_text(query)} "
    if ' mod ' in hay and ' mod ' not in query_n and ' hack ' not in query_n and ' romhack ' not in query_n:
        return True
    if ' dlc ' in hay and ' dlc ' not in query_n:
        return True
    return any(token in hay for token in BAD_RESULT_TITLE_TOKENS)

def query_terms(text):
    out=[]
    for tok in normalize_search_text(text).split():
        if len(tok) < 2 and tok not in ROMAN_NUMERAL_TOKENS and not tok.isdigit():
            continue
        if tok in SEARCH_STOPWORDS:
            continue
        out.append(tok)
    return out


def query_number_token(terms):
    for tok in reversed(terms):
        if tok.isdigit() or tok in ROMAN_NUMERAL_TOKENS:
            return tok
    return ''


def title_starts_with_terms(title_terms, wanted_terms):
    if not wanted_terms or len(title_terms) < len(wanted_terms):
        return False
    return all(token_fuzzy_match(tok, title_terms[idx]) for idx, tok in enumerate(wanted_terms))


def first_non_year_token(tokens):
    for tok in tokens:
        if tok.isdigit() and len(tok) == 4:
            continue
        return tok
    return ''


def query_matches_title(title, q):
    q_norm = normalize_search_text(q)
    q_terms = query_terms(q)
    if not q_terms:
        return True
    title_n_clean = normalize_search_text(title)
    title_n = f" {title_n_clean} "
    if q_norm and f' {q_norm} ' in title_n:
        return True
    matched = [tok for tok in q_terms if token_fuzzy_match(tok, title_n_clean)]
    distinctive = [tok for tok in q_terms if tok not in NON_DISTINCTIVE_QUERY_TOKENS]
    if any(tok in ROMAN_NUMERAL_TOKENS for tok in distinctive):
        if not any(tok in matched for tok in ROMAN_NUMERAL_TOKENS):
            return False
    if not distinctive:
        return bool(matched)
    matched_distinctive = sum(1 for tok in distinctive if tok in matched)
    query_word_count = len(q_norm.split())
    if query_word_count >= 2 and len(distinctive) == 1:
        return False
    return matched_distinctive / len(distinctive) >= 0.6

def result_score(title, q, platform='', seeders=None):
    title_n_clean = normalize_search_text(title)
    title_n = f" {title_n_clean} "
    query_n = normalize_search_text(q)
    q_terms = query_terms(q)
    title_terms = query_terms(title)
    target_num = query_number_token(q_terms)
    base_terms = q_terms[:-1] if target_num and q_terms else q_terms
    score = 0

    if query_n and f' {query_n} ' in title_n:
        score += 180

    prefix_terms = q_terms if title_starts_with_terms(title_terms, q_terms) else []
    if prefix_terms:
        score += 180
    elif base_terms and title_starts_with_terms(title_terms, base_terms):
        score += 100

    if q_terms:
        title_prefix = ' '.join(title_terms[:len(q_terms)])
        if title_prefix:
            score += int(difflib.SequenceMatcher(None, ' '.join(q_terms), title_prefix).ratio() * 80)

    remainder = title_terms[len(base_terms):] if base_terms and title_starts_with_terms(title_terms, base_terms) else []
    leading_tok = remainder[0] if remainder else ''
    next_tok = first_non_year_token(remainder)

    if target_num:
        if remainder and remainder[0] == target_num:
            score += 160
            tail = remainder[1:]
            if tail and tail[0].isdigit() and len(tail[0]) == 4:
                score += 120
            tail_next = first_non_year_token(tail)
            if not tail_next:
                score += 80
            elif tail_next == 'exe':
                score -= 60
            elif tail_next in REPACK_RESULT_TOKENS:
                score -= 20
            elif tail_next in COLLECTION_RESULT_TOKENS:
                score -= 90
            elif tail_next in SPINOFF_RESULT_TOKENS:
                score -= 110
        elif target_num in remainder:
            score -= 100
        elif target_num in title_terms:
            score -= 140
    else:
        if leading_tok.isdigit() and len(leading_tok) == 4:
            score += 140
        elif not next_tok and remainder:
            score += 120
        elif not next_tok:
            score += 100
        elif next_tok in COLLECTION_RESULT_TOKENS:
            score -= 130
        elif next_tok in SPINOFF_RESULT_TOKENS:
            score -= 110
        elif next_tok == 'exe':
            score -= 60
        elif next_tok in REPACK_RESULT_TOKENS:
            score -= 20
        elif next_tok.isdigit():
            score += 40 if len(next_tok) == 4 else -140

    matched_terms = 0
    for tok in q_terms:
        if f' {tok} ' in title_n:
            score += 16
            matched_terms += 1
        elif token_fuzzy_match(tok, title_n_clean):
            score += 10
            matched_terms += 1
    if q_terms and matched_terms == len(q_terms):
        score += 30

    extra_terms = max(0, len(title_terms) - len(q_terms))
    score -= min(extra_terms, 8) * 5

    platform_n = normalize_search_text(platform)
    if platform_n and f' {platform_n} ' in title_n:
        score += 18

    try:
        seeders_n = int(seeders)
    except Exception:
        seeders_n = -1
    if seeders_n > 0:
        score += min(seeders_n, 12)
    elif seeders_n == 0:
        score -= 40

    if is_obvious_junk_title(title, q):
        score -= 200
    return score

def has_game_category(cats):
    names = [c.lower() for c in flatten_cats(cats)]
    ids = category_ids(cats)
    return (
        any(n.startswith('console') for n in names)
        or any(n.startswith('pc/games') or n == 'pc' for n in names)
        or any(n.startswith('pc/mobile-android') for n in names)
        or 4070 in ids
        or any(1000 <= cid < 1200 for cid in ids)
    )


def is_game_result(x):
    cats = flatten_cats(x.get('categories'))
    cat_l = [c.lower() for c in cats]
    if any(c.startswith(BAD_CATEGORY_PREFIXES) for c in cat_l):
        return False
    if has_game_category(x.get('categories')):
        return True
    # Last-resort safety net for indexers that return sparse categories, but only for
    # obvious console/ROM/PC-game result titles. This avoids accepting Movies/TV/Audio junk.
    hay = ' '.join([x.get('title',''), x.get('indexer','')] + [c or '' for c in cats]).lower()
    if 'pc/game' in hay or 'windows' in hay or 'linux' in hay or 'dos' in hay:
        return True
    return any(tok in hay for tok in CONSOLE_TITLE_TOKENS)

def platform_aliases(platform):
    for folder, aliases in PLATFORM_ALIASES:
        if folder == platform:
            return aliases
    return []

def matches_platform(x, platform):
    if not platform:
        return True
    cats = flatten_cats(x.get('categories'))
    cat_l = [c.lower() for c in cats]
    ids = set(category_ids(x.get('categories')))
    wanted_ids = set(PLATFORM_CATEGORY_IDS.get(platform, ()))
    if wanted_ids and ids.intersection(wanted_ids):
        return True
    hay = ' '.join([x.get('title',''), x.get('indexer','')] + cats).lower()
    hay_padded = f' {hay} '
    hay_norm = f" {normalize_search_text(hay)} "
    if platform in ('Windows', 'Linux', 'DOS'):
        has_pc_hint = any(tok in hay for tok in PC_RESULT_HINTS) or any(tok in hay_norm for tok in (' windows ', ' pc games ', ' pc game ', ' gog ', ' steam ', ' linux ', ' dos '))
        has_console_hint = any(tok in hay_norm for tok in CONSOLE_ONLY_HINTS)
        has_console_category = any(c.startswith('console') for c in cat_l)
        if (has_console_hint or has_console_category) and not has_pc_hint:
            return False
        if has_pc_hint:
            return True
    if platform.lower() in hay:
        return True
    if any(alias.lower() in hay for alias in platform_aliases(platform)):
        return True

    # For platforms that are not part of the configured Prowlarr console category map
    # (for example Windows/Linux/DOS folders), do not hard-fail the result. Keeping
    # these results visible is preferable to returning an empty result set for a valid
    # folder selection, because platform hints are already weak for these folders.
    return not wanted_ids

def guess_result_platform(x):
    cats = flatten_cats(x.get('categories'))
    title_hay = ' '.join([x.get('title',''), x.get('indexer','')]).lower()
    hay = ' '.join([x.get('title',''), x.get('indexer','')] + cats).lower()
    hay_norm = f" {normalize_search_text(hay)} "
    if ' android ' in hay_norm or any(token in hay for token in ('.apk', '.xapk', '.apkm', '.obb')):
        return 'Android'
    if 'ms-dos' in hay or ' dos ' in f' {hay} ' or 'dosbox' in hay:
        return 'DOS'
    if any(tok in hay for tok in ('gnu/linux', ' linux ', 'linux nfo', 'linux build')):
        return 'Linux'
    if any(tok in hay for tok in ('windows', 'pc/games', 'pc game', 'steam', '.exe', '.msi')):
        return 'Windows'
    console_cat_count = sum(1 for c in cats if (c or '').lower().startswith('console/'))
    console_hint_tokens = ('android', 'switch', 'wii', 'wii u', 'xbox', 'playstation', 'ps1', 'ps2', 'ps3', 'ps4', 'ps5', 'psp', 'vita', 'nds', '3ds', 'nintendo', 'gameboy', 'game boy', 'genesis', 'dreamcast', 'saturn', 'atari', 'neo geo', 'quest', 'meta quest', 'vr')
    if console_cat_count >= 4 and not any(tok in title_hay for tok in console_hint_tokens):
        return 'Windows'
    return ''


def result_platform_label(x, requested_platform=''):
    if requested_platform:
        return requested_platform
    guessed = guess_result_platform(x)
    if guessed:
        return guessed
    for cat in flatten_cats(x.get('categories')):
        cat_l = (cat or '').lower()
        if cat_l.startswith('console/'):
            return cat.split('/', 1)[1]
        if cat_l == 'console':
            return 'Console'
        if cat_l.startswith('pc/games'):
            return 'Windows'
        if cat_l == 'pc':
            return 'PC'
    return ''

def search_category_ids(platform):
    # Keep the broad console bucket, but we also run an all-category pass because some
    # real game results land in PC/Games or other non-console buckets on the indexer.
    return PROWLARR_CONSOLE_CATEGORIES


def query_variants(q):
    variants = []

    def add(value):
        value = (value or '').strip()
        if value and value not in variants:
            variants.append(value)

    add(q)
    q_norm = normalize_search_text(q)
    if q_norm:
        add(q_norm)
        add(q_norm.replace('freddys', "freddy's"))
    terms = query_terms(q)
    if terms:
        add(' '.join(terms))
    distinctive = [tok for tok in terms if tok not in NON_DISTINCTIVE_QUERY_TOKENS]
    if len(distinctive) >= 3:
        tail = [tok for tok in distinctive if tok.isdigit() or tok in ROMAN_NUMERAL_TOKENS]
        if tail:
            add(f"{distinctive[0]} {distinctive[-2]} {tail[-1]}")
            add(f"{distinctive[0]} {distinctive[-1]}")
        else:
            add(f"{distinctive[0]} {distinctive[-1]}")
    return variants


def query_with_platform(q, platform):
    if not platform:
        return q
    # Keep the query broad; platform filtering happens in result-post-processing.
    # Appending platform terms to the search query (especially for values like
    # `Windows`, `Linux`, `DOS`, `Xbox 360`) can over-constrain the indexer query
    # and return zero results before local filtering has a chance to run.
    return q


def prowlarr_params(q, platform='', categories=None):
    params=[('query', query_with_platform(q, platform)), ('type', 'search'), ('apikey', PROWLARR_API_KEY)]
    for cat in categories or ():
        params.append(('categories', str(cat)))
    for indexer_id in GAMEFINDER_INDEXER_IDS:
        params.append(('indexerIds', indexer_id))
    return params


def result_key(x):
    return (x.get('guid') or x.get('downloadUrl') or x.get('magnetUrl') or x.get('title') or '').lower()


def fetch_prowlarr(q, platform='', categories=None):
    try:
        r=requests.get(f'{PROWLARR_URL}/api/v1/search', params=prowlarr_params(q, platform, categories), timeout=PROWLARR_SEARCH_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return []


def prowlarr_search(q, platform=''):
    raw=[]
    seen=set()
    searches = [(q, search_category_ids(platform)), (q, ())]
    need_fallback = True
    for variant_q, categories in searches:
        rows = fetch_prowlarr(variant_q, platform, categories)
        if rows:
            need_fallback = False
        for x in rows:
            key=result_key(x)
            if key in seen:
                continue
            seen.add(key)
            raw.append(x)
    if need_fallback or len(raw) < max(40, RESULTS_PER_PAGE):
        for variant_q in query_variants(q)[1:4]:
            for categories in (search_category_ids(platform), ()):
                for x in fetch_prowlarr(variant_q, platform, categories):
                    key=result_key(x)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw.append(x)
    out=[]
    for x in raw:
        if not is_game_result(x) or not matches_platform(x, platform):
            continue
        title = x.get('title','')
        if is_obvious_junk_title(title, q) or not query_matches_title(title, q):
            continue
        url=x.get('magnetUrl') or x.get('downloadUrl')
        if not url:
            continue
        seeders = x.get('seeders')
        peers = x.get('peers')
        if peers is None:
            peers = x.get('leechers')
        try:
            seeders_sort = int(seeders)
        except Exception:
            seeders_sort = -1
        if seeders_sort == 0:
            continue
        result_platform = result_platform_label(x, platform)
        out.append({
            'title': title,
            'indexer':x.get('indexer',''),
            'size_h':human(x.get('size')),
            'protocol':x.get('protocol',''),
            'url':url,
            'categories':flatten_cats(x.get('categories')),
            'platform': result_platform,
            'seeders': '?' if seeders is None else seeders,
            'peers': '?' if peers is None else peers,
            '_score': result_score(title, q, platform, seeders),
            '_seeders_sort': seeders_sort,
            '_peers_sort': int(peers or 0) if str(peers or '').isdigit() else -1,
            '_size_sort': int(x.get('size') or 0),
            '_indexer_sort': normalize_search_text(x.get('indexer','')),
            '_platform_sort': normalize_search_text(result_platform),
        })
    out.sort(key=lambda r: (r['_score'], r['_seeders_sort'], r['_size_sort']), reverse=True)
    return out


def sort_search_results(results, sort_by='relevance'):
    choice = normalize_sort_choice(sort_by)
    rows = list(results or [])
    if choice == 'seeders':
        rows.sort(key=lambda r: (r.get('_seeders_sort', -1), r.get('_peers_sort', -1), r.get('_size_sort', 0), r.get('_score', -10**9)), reverse=True)
    elif choice == 'peers':
        rows.sort(key=lambda r: (r.get('_peers_sort', -1), r.get('_seeders_sort', -1), r.get('_size_sort', 0), r.get('_score', -10**9)), reverse=True)
    elif choice == 'tracker':
        rows.sort(key=lambda r: (r.get('_indexer_sort', ''), -r.get('_score', 0), -r.get('_seeders_sort', -1), -r.get('_size_sort', 0)))
    elif choice == 'platform':
        rows.sort(key=lambda r: (r.get('_platform_sort', ''), -r.get('_score', 0), -r.get('_seeders_sort', -1), -r.get('_size_sort', 0)))
    elif choice == 'size':
        rows.sort(key=lambda r: (r.get('_size_sort', 0), r.get('_seeders_sort', -1), r.get('_peers_sort', -1), r.get('_score', -10**9)), reverse=True)
    else:
        rows.sort(key=lambda r: (r.get('_score', -10**9), r.get('_seeders_sort', -1), r.get('_size_sort', 0)), reverse=True)
    return rows


def public_search_results(results):
    cleaned = []
    for row in results or []:
        cleaned.append({k: v for k, v in row.items() if not k.startswith('_')})
    return cleaned


def clean_search_query(value):
    value = (value or '').replace('\xa0', ' ')
    value = value.replace('\r', ' ').replace('\n', ' ')
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def fallback_search_queries(title, search_q=''):
    queries = []

    def add(value):
        value = clean_search_query(value)
        if value and value not in queries:
            queries.append(value)

    add(title)
    first_line = clean_search_query((title or '').splitlines()[0] if title else '')
    add(first_line)
    if first_line:
        add(re.sub(r'\([^)]*\)', ' ', first_line))
    add(search_q)
    return queries


def fallback_result_candidates(title, platform='', failed_url='', failed_indexer='', search_q=''):
    title_query = clean_search_query((title or '').splitlines()[0] if title else '')
    expected_query = title_query or clean_search_query(search_q or title)
    candidates = []
    seen = {failed_url} if failed_url else set()
    for query in fallback_search_queries(title, search_q):
        for row in prowlarr_search(query, platform):
            url = row.get('url')
            if not url or url in seen:
                continue
            seen.add(url)
            if expected_query and not query_matches_title(row.get('title', ''), expected_query):
                continue
            candidates.append(row)
        if candidates:
            break
    candidates.sort(key=lambda row: (
        row.get('indexer', '') == failed_indexer,
        -(row.get('_score', 0) or 0),
        -(row.get('_seeders_sort', -1) or -1),
        -(row.get('_size_sort', 0) or 0),
    ))
    return candidates


def preferred_add_platform(platform='', title=''):
    platform = (platform or '').strip()
    if platform and platform not in {'Other', 'Console'}:
        return platform
    inferred = infer_platform(title, title)
    return inferred or platform


def add_torrent_with_fallback(url, title, platform='', indexer='', search_q=''):
    attempts = [
        {
            'title': title,
            'platform': preferred_add_platform(platform, title),
            'indexer': indexer,
            'url': url,
            'used_fallback': False,
        }
    ]
    errors = []
    for attempt in attempts:
        try:
            attempt['hash'] = add_torrent(attempt['url'], attempt['title'], attempt['platform'], attempt['indexer'])
            return attempt
        except Exception as e:
            errors.append(f"{attempt.get('indexer') or 'selected result'}: {e}")
            for row in fallback_result_candidates(title, platform, attempt['url'], indexer, search_q):
                alt = {
                    'title': row.get('title', title),
                    'platform': preferred_add_platform(platform or row.get('platform', ''), row.get('title', title)),
                    'indexer': row.get('indexer', ''),
                    'url': row.get('url', ''),
                    'used_fallback': True,
                }
                try:
                    alt['hash'] = add_torrent(alt['url'], alt['title'], alt['platform'], alt['indexer'])
                    return alt
                except Exception as alt_error:
                    errors.append(f"{alt.get('indexer') or 'alternate result'}: {alt_error}")
            break
    if len(errors) == 1:
        raise RuntimeError(errors[0])
    raise RuntimeError('Selected result failed, and alternate matches failed too: ' + ' | '.join(errors[:4]))


def qbit_post(path, data=None, files=None):
    r=requests.post(f'{QBIT_URL}/api/v2/{path}', data=data, files=files, timeout=30)
    r.raise_for_status()
    return r


def qbit_add(data=None, files=None):
    r = requests.post(f'{QBIT_URL}/api/v2/torrents/add', data=data, files=files, timeout=30)
    if r.status_code not in (200, 409):
        r.raise_for_status()
    return r

def qbit_get(path, params=None):
    r=requests.get(f'{QBIT_URL}/api/v2/{path}', params=params, timeout=30)
    r.raise_for_status()
    return r

def ensure_category():
    # qBittorrent returns 409 if the category already exists; that is fine.
    r = requests.post(f'{QBIT_URL}/api/v2/torrents/createCategory', data={'category':QBIT_CATEGORY,'savePath':QBIT_SAVE_PATH}, timeout=30)
    if r.status_code not in (200, 409):
        r.raise_for_status()
    qbit_post('torrents/editCategory', data={'category':QBIT_CATEGORY,'savePath':QBIT_SAVE_PATH})

def add_torrent(url, title, platform, indexer):
    ensure_category()
    before = {t.get('hash','').lower() for t in qbit_get('torrents/info', params={'category':QBIT_CATEGORY}).json() if t.get('hash')}
    data={'category':QBIT_CATEGORY,'savepath':QBIT_SAVE_PATH}
    if url.lower().startswith('magnet:'):
        resp = qbit_add(data={**data, 'urls':url})
    else:
        # Do not make qBittorrent fetch LAN/Prowlarr URLs from inside its VPN namespace.
        # Fetch the .torrent in GameFinder and upload the torrent bytes directly to qBit.
        # Some indexers/Prowlarr endpoints redirect to magnet: links; catch that redirect
        # and pass the magnet to qBit instead of letting requests try to fetch it.
        tr = requests.get(url, timeout=(15, 90), allow_redirects=False)
        if 300 <= tr.status_code < 400 and tr.headers.get('Location','').lower().startswith('magnet:'):
            resp = qbit_add(data={**data, 'urls':tr.headers['Location']})
        else:
            tr.raise_for_status()
            if not tr.content or len(tr.content) < 20:
                raise RuntimeError('Prowlarr returned an empty torrent file')
            files={'torrents': (safe_name(title)+'.torrent', tr.content, 'application/x-bittorrent')}
            resp = qbit_add(data=data, files=files)
    # qBittorrent 5 returns JSON; treat zero success + no visible new torrent as a real failure.
    add_text = (resp.text or '').strip()
    h=''
    last_arr=[]
    for _ in range(12):
        time.sleep(1)
        arr=qbit_get('torrents/info', params={'category':QBIT_CATEGORY}).json()
        last_arr=arr
        new=[t for t in arr if t.get('hash','').lower() not in before]
        candidates=new or arr
        for t in sorted(candidates, key=lambda x:x.get('added_on',0), reverse=True):
            name=(t.get('name') or '').lower()
            if new or title.lower()[:25] in name or name[:25] in title.lower():
                h=t.get('hash','')
                break
        if h:
            break
    if not h and resp.status_code == 409:
        for t in sorted(last_arr, key=lambda x:x.get('added_on',0), reverse=True):
            name=(t.get('name') or '').lower()
            if title.lower()[:25] in name or name[:25] in title.lower():
                h=t.get('hash','')
                break
    if not h:
        raise RuntimeError(f'qBittorrent accepted request but no ROM torrent appeared in category {QBIT_CATEGORY}. Response: {add_text[:200] or "empty"}')
    # Explicitly keep ROM jobs seeding indefinitely, regardless of future global/category changes.
    qbit_post('torrents/setShareLimits', data={'hashes':h, 'ratioLimit':'-1', 'seedingTimeLimit':'-1', 'inactiveSeedingTimeLimit':'-1', 'shareLimitAction':'0'})
    qbit_post('torrents/start', data={'hashes':h})
    con=db()
    existing_job = con.execute('select id from jobs where lower(hash)=lower(?) order by id desc limit 1', (h,)).fetchone()
    if not existing_job:
        con.execute('insert into jobs(title,platform,indexer,url,hash,status,message) values(?,?,?,?,?,?,?)',(title,platform,indexer,url,h,'downloading','Sent to qBittorrent with unlimited seeding; waiting for download progress.'))
        con.commit()
    con.close()
    return h

def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+','_',s).strip()[:180] or 'downloaded-rom'

def qbit_by_hash():
    try:
        arr = qbit_get('torrents/info', params={'category':QBIT_CATEGORY}).json()
        return {t.get('hash','').lower(): t for t in arr if t.get('hash')}
    except Exception:
        return {}

def enrich_jobs(rows):
    torrents = qbit_by_hash()
    enriched=[]
    for row in rows:
        j=dict(row)
        h=(j.get('hash') or '').lower()
        t=torrents.get(h)
        j['hash_short']=(h[:10]+'…') if h else 'hash pending'
        j['progress_pct']=0
        j['download_label']='Queued'
        j['qbit_state']='not seen yet'
        j['dlspeed']='0 B/s'
        j['upspeed']='0 B/s'
        j['save_path']=''
        if t:
            pct=round(float(t.get('progress') or 0)*100, 1)
            j['progress_pct']=min(100, max(0, pct))
            j['qbit_state']=t.get('state') or ''
            j['dlspeed']=speed(t.get('dlspeed') or 0)
            j['upspeed']=speed(t.get('upspeed') or 0)
            j['save_path']=t.get('content_path') or t.get('save_path') or ''
            if pct >= 100:
                j['download_label']='Downloaded'
            else:
                j['download_label']='Downloading'
        if j.get('status') == 'complete':
            j['download_label']='Downloaded'
            j['progress_pct']=100
        enriched.append(j)
    return enriched

def eta_text(seconds):
    try: seconds=int(seconds or 0)
    except Exception: return ''
    if seconds <= 0 or seconds >= 8640000: return '∞'
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h: return f'{h}h {m}m'
    if m: return f'{m}m {s}s'
    return f'{s}s'

def qbit_rom_rows():
    try:
        arr=qbit_get('torrents/info', params={'category':QBIT_CATEGORY}).json()
    except Exception as e:
        return [{'name':'qBittorrent API error', 'hash_short':'', 'state':str(e), 'progress_pct':0, 'seeds':'-', 'peers':'-', 'dlspeed':'0 B/s', 'upspeed':'0 B/s', 'eta':'', 'path':''}]
    rows=[]
    for t in sorted(arr, key=lambda x:x.get('added_on',0), reverse=True):
        h=(t.get('hash') or '').lower()
        rows.append({
            'name': t.get('name') or '(unnamed)',
            'hash_short': (h[:10]+'…') if h else '',
            'state': t.get('state') or '',
            'progress_pct': round(float(t.get('progress') or 0)*100, 1),
            'seeds': f"{t.get('num_seeds',0)} ({t.get('num_complete',0)})",
            'peers': t.get('num_leechs',0),
            'dlspeed': speed(t.get('dlspeed') or 0),
            'upspeed': speed(t.get('upspeed') or 0),
            'eta': eta_text(t.get('eta')),
            'path': t.get('content_path') or t.get('save_path') or '',
        })
    return rows

def infer_platform(title='', path=''):
    hay = f'{title} {path}'.lower()
    ext = Path(path).suffix.lower()
    available = {value for value, _label in platform_options() if value}
    for folder in sorted(available, key=len, reverse=True):
        if folder.lower() in hay:
            return folder
    for folder, aliases in PLATFORM_ALIASES:
        if folder in available and any(alias in hay for alias in aliases):
            return folder
    if ext in ANDROID_PAYLOAD_EXTS or ' android ' in f' {hay} ':
        return 'Android' if 'Android' in available else ''
    if ext in ('.nsp', '.nsz', '.xci', '.xcz') or ' nintendo switch' in hay or ' switch' in f' {hay} ':
        return 'Nintendo Switch'
    if ext == '.nds' or '[nds]' in hay or ' nintendo ds' in hay or ' nds' in hay:
        return 'nds'
    if 'ms-dos' in hay or ' dos ' in f' {hay} ' or 'dosbox' in hay:
        return 'DOS'
    if 'gnu/linux' in hay or ' linux ' in f' {hay} ':
        return 'Linux'
    if ext in ('.exe','.msi') or ' windows ' in f' {hay} ' or ' pc/games ' in f' {hay} ' or ' pc game ' in f' {hay} ' or ' steam ' in f' {hay} ':
        return 'Windows'
    if ext == '.gba' or 'gameboy advance' in hay or 'game boy advance' in hay or ' gba' in hay:
        return 'Nintendo GameBoy Advance'
    if ext == '.gbc' or 'gameboy color' in hay or 'game boy color' in hay or ' gbc' in hay:
        return 'Nintendo GameBoy Color'
    if ext == '.gb' or 'gameboy' in hay or 'game boy' in hay:
        return 'Nintendo GameBoy'
    if ext in ('.wbfs','.rvz','.gcm') or ' nintendo wii' in hay or ' wii' in hay:
        return 'Nintendo Wii'
    if ext in ('.iso','.chd','.cue','.bin') and ('playstation 2' in hay or ' ps2' in hay):
        return 'PlayStation 2'
    if ext in ('.iso','.chd','.cue','.bin') and ('playstation' in hay or ' ps1' in hay):
        return 'PlayStation 1'
    if 'xbox 360' in hay or '[xbox 360]' in hay or ' xbox360' in hay:
        return 'Xbox 360'
    if ext in ('.n64','.z64','.v64') or ' nintendo 64' in hay or ' n64' in hay:
        return 'Nintendo 64'
    if ext in ('.sfc','.smc') or ' snes' in hay or 'super nintendo' in hay:
        return 'snes'
    if ext == '.nes' or ' nes' in hay:
        return 'Nintendo Entertainment System'
    return ''

def platform_dir(platform):
    if platform and (ROMS_ROOT/platform).exists(): return ROMS_ROOT/platform
    return None

def romm_roms_dir(platform):
    root = platform_dir(platform)
    if root is None:
        raise RuntimeError('Could not determine the console folder for this game. Pick the correct platform in GameFinder and add it again, or rename the result with a console tag like [NDS], [Xbox 360], [PS2], etc. Nothing was copied into Unsorted.')
    roms = root/'roms'
    return roms if roms.exists() else root

def keep_seeding_copy(src: Path, final: Path):
    """Expose a completed ROM to RomM without moving qBittorrent's source file.

    Moving the source out of qBittorrent's save path breaks rechecks/seeding. Prefer a
    hardlink on the same filesystem, then fall back to a normal copy if hardlinks are
    not supported. For extracted archive contents, src is already a separate extracted
    file so this still leaves the original torrent payload untouched.
    """
    if src.resolve() == final.resolve():
        return final, 'already in place'
    if final.exists():
        final = final.with_name(safe_name(final.stem) + '-' + str(int(time.time())) + final.suffix)
    if src.is_dir():
        counts = {'hardlinked': 0, 'copied': 0}
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            target_root = final / rel
            target_root.mkdir(parents=True, exist_ok=True)
            for name in files:
                source_file = Path(root) / name
                target_file = target_root / name
                try:
                    os.link(source_file, target_file)
                    counts['hardlinked'] += 1
                except OSError:
                    shutil.copy2(source_file, target_file)
                    counts['copied'] += 1
        if counts['copied']:
            return final, f'copied folder payload for RomM ({counts["copied"]} copied, {counts["hardlinked"]} hardlinked); qBittorrent source left in place for seeding'
        return final, f'hardlinked folder payload for RomM ({counts["hardlinked"]} files); qBittorrent source left in place for seeding'
    try:
        os.link(src, final)
        return final, 'hardlinked for RomM; qBittorrent source left in place for seeding'
    except OSError:
        shutil.copy2(src, final)
        return final, 'copied for RomM; qBittorrent source left in place for seeding'

def extract_if_needed(path: Path, prefer_dir=False):
    if path.suffix.lower() not in ARCHIVE_EXTS: return path
    dest=path.parent/(path.stem+'_extracted')
    dest.mkdir(exist_ok=True)
    if path.suffix.lower()=='.zip':
        subprocess.run(['unzip','-o',str(path),'-d',str(dest)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif path.suffix.lower()=='.7z':
        subprocess.run(['7z','x','-y',f'-o{dest}',str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif path.suffix.lower()=='.rar':
        subprocess.run(['unrar','x','-o+',str(path),str(dest)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files=[p for p in dest.rglob('*') if p.is_file() and p.suffix.lower() in ROM_EXTS]
    extracted_files = [p for p in dest.rglob('*') if p.is_file()]
    if prefer_dir and extracted_files:
        return dest
    if len(files) > 1:
        return dest
    if files:
        return max(files, key=lambda p:p.stat().st_size)
    if prefer_dir and extracted_files:
        return dest
    return path

def unwrap_single_child_dir(path: Path):
    current = path
    while current.is_dir():
        children = [p for p in current.iterdir() if not p.name.startswith('.')]
        dirs = [p for p in children if p.is_dir()]
        files = [p for p in children if p.is_file()]
        if len(dirs) == 1 and not files:
            current = dirs[0]
            continue
        break
    return current

def bundle_sources(path: Path, platform: str):
    if not path.is_dir():
        return []
    root = unwrap_single_child_dir(path)
    if platform in ('Windows', 'Linux', 'DOS'):
        # Keep desktop-style game payloads together as a single RomM item.
        # Splitting top-level bundle contents turns one game into several RomM rows
        # and can drop required sidecar files like UnityPlayer.dll.
        return []
    if platform == 'Android':
        entries = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith('.'):
                continue
            if child.is_dir() or child.suffix.lower() in EXECUTABLE_EXTS or child.suffix.lower() in ARCHIVE_EXTS or child.suffix.lower() in ANDROID_PAYLOAD_EXTS:
                entries.append(child)
        return entries if len(entries) > 1 else []
    if platform == 'Nintendo Switch':
        entries = sorted(
            [p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in {'.nsp', '.nsz', '.xci', '.xcz'}],
            key=lambda p: str(p).lower(),
        )
        return entries if len(entries) > 1 else []
    return []

def import_many_into_romm(sources, platform: str, final_dir: Path):
    imported = []
    copy_notes = []
    prefer_dir_extract = platform in ('Windows', 'Linux', 'DOS', 'Android')
    for source in sources:
        prepared = extract_if_needed(source, prefer_dir=prefer_dir_extract) if source.is_file() else source
        final = final_dir / safe_name(prepared.name)
        final_path, copy_msg = keep_seeding_copy(prepared, final)
        imported.append(str(final_path))
        copy_notes.append(f'{prepared.name}: {copy_msg}')
    return imported, copy_notes

def import_target_name(prepared: Path, platform: str, job_title: str = ''):
    title_clean = clean_search_query((job_title or '').splitlines()[0])
    if platform in ('Windows', 'Linux', 'DOS') and prepared.is_dir() and title_clean:
        return safe_name(title_clean)
    if platform == 'Android' and prepared.is_file() and prepared.suffix.lower() in ANDROID_PAYLOAD_EXTS:
        stem_n = normalize_search_text(prepared.stem)
        if stem_n in {'app', 'game', 'setup', 'install', 'release', 'android', 'lego'} or len(stem_n) <= 4:
            if title_clean:
                return safe_name(title_clean) + prepared.suffix.lower()
    return safe_name(prepared.name)


def build_import_plan(src: Path, platform: str, final_dir: Path, job_title: str = ''):
    prefer_dir_extract = platform in ('Windows', 'Linux', 'DOS', 'Android')
    sources = bundle_sources(src, platform) or [src]
    plan = []
    for source in sources:
        prepared = extract_if_needed(source, prefer_dir=prefer_dir_extract) if source.is_file() else source
        plan.append((prepared, final_dir / import_target_name(prepared, platform, job_title)))
    return plan

def import_plan_into_romm(plan):
    imported = []
    copy_notes = []
    for prepared, final in plan:
        final_path, copy_msg = keep_seeding_copy(prepared, final)
        imported.append(str(final_path))
        copy_notes.append(f'{prepared.name}: {copy_msg}')
    return imported, copy_notes

def summarize_import(copy_notes, platform: str, count: int):
    if count <= 1:
        return copy_notes[0] if copy_notes else f'imported into /roms/{platform}/roms'
    summary = f'imported {count} bundle items into /roms/{platform}/roms; ' + '; '.join(copy_notes[:4])
    if len(copy_notes) > 4:
        summary += f'; and {len(copy_notes) - 4} more'
    return summary

def romm_target_verified(target: Path, platform: str):
    con = romm_db_conn()
    if con is None:
        return False
    try:
        rel_parent = str(target.parent.relative_to(ROMS_ROOT)).replace(os.sep, '/')
        with con.cursor() as cur:
            if target.is_dir():
                rel = str(target.relative_to(ROMS_ROOT)).replace(os.sep, '/')
                cur.execute('''
                    select count(*) hits
                    from rom_files rf
                    join roms r on r.id = rf.rom_id
                    join platforms p on p.id = r.platform_id
                    where p.name=%s and rf.file_path=%s and rf.missing_from_fs=0 and r.missing_from_fs=0
                ''', (platform, rel))
                row = cur.fetchone() or {}
                if int(row.get('hits') or 0) > 0:
                    return True
                cur.execute('''
                    select count(*) hits
                    from roms r
                    join platforms p on p.id = r.platform_id
                    where p.name=%s and r.fs_path=%s and r.fs_name=%s and r.missing_from_fs=0
                ''', (platform, rel_parent, target.name))
                row = cur.fetchone() or {}
                return int(row.get('hits') or 0) > 0
            cur.execute('''
                select count(*) hits
                from rom_files rf
                join roms r on r.id = rf.rom_id
                join platforms p on p.id = r.platform_id
                where p.name=%s and rf.file_path=%s and rf.file_name=%s and rf.missing_from_fs=0 and r.missing_from_fs=0
            ''', (platform, rel_parent, target.name))
            row = cur.fetchone() or {}
            if int(row.get('hits') or 0) > 0:
                return True
            cur.execute('''
                select count(*) hits
                from roms r
                join platforms p on p.id = r.platform_id
                where p.name=%s and r.fs_path=%s and r.fs_name=%s and r.missing_from_fs=0
            ''', (platform, rel_parent, target.name))
            row = cur.fetchone() or {}
            return int(row.get('hits') or 0) > 0
    except Exception as e:
        print('romm verify error', e, flush=True)
        return False
    finally:
        con.close()

def romm_verify_imports(imported_paths, platform: str):
    verified = []
    missing = []
    for raw in imported_paths:
        target = Path(raw)
        if romm_target_verified(target, platform):
            verified.append(raw)
        else:
            missing.append(raw)
    return verified, missing


def slugify_name(name: str):
    slug = normalize_search_text(name).replace(' ', '-')
    return slug[:400] or None


def sort_key_for_name(name: str):
    # Match RomM's simple normalized title sort well enough for exact-title imports.
    return normalize_search_text(name)[:500] or None


def romm_import_rows_for_target(target: Path, platform: str):
    con = romm_db_conn()
    if con is None:
        return []
    try:
        rel_parent = str(target.parent.relative_to(ROMS_ROOT)).replace(os.sep, '/')
        rows = []
        with con.cursor() as cur:
            cur.execute('''
                select distinct r.id, r.name, r.fs_name, r.fs_path
                from roms r
                join platforms p on p.id = r.platform_id
                where p.name=%s and r.fs_path=%s and r.fs_name=%s and r.missing_from_fs=0
            ''', (platform, rel_parent, target.name))
            rows.extend(cur.fetchall() or [])
            if target.is_dir():
                rel = str(target.relative_to(ROMS_ROOT)).replace(os.sep, '/')
                cur.execute('''
                    select distinct r.id, r.name, r.fs_name, r.fs_path
                    from rom_files rf
                    join roms r on r.id = rf.rom_id
                    join platforms p on p.id = r.platform_id
                    where p.name=%s and rf.file_path=%s and rf.missing_from_fs=0 and r.missing_from_fs=0
                ''', (platform, rel))
                rows.extend(cur.fetchall() or [])
        seen = set()
        out = []
        for row in rows:
            rid = row.get('id')
            if rid in seen:
                continue
            seen.add(rid)
            out.append(row)
        return out
    except Exception as e:
        print('romm import row lookup error', e, flush=True)
        return []
    finally:
        con.close()


def romm_pin_single_import_title(imported_paths, platform: str, job_title: str):
    """Keep exact single-title GameFinder imports searchable by the title the user added.

    RomM's metadata matcher can misidentify dotted Windows bundle folders such as
    I.Am.Cat_extracted as an unrelated game (for example "I Am 1"). When a
    GameFinder job imported exactly one item, prefer the clean job title for the
    RomM display/search fields after the filesystem import is verified.
    """
    title = clean_search_query((job_title or '').splitlines()[0])
    if not title or len(imported_paths or []) != 1:
        return []
    if platform not in ('Windows', 'Linux', 'DOS', 'Android'):
        return []
    target = Path(imported_paths[0])
    rows = romm_import_rows_for_target(target, platform)
    if not rows:
        return []
    changed = []
    con = romm_db_conn()
    if con is None:
        return []
    try:
        with con.cursor() as cur:
            for row in rows:
                current = row.get('name') or ''
                # If RomM already contains the requested words, leave its richer metadata alone.
                wanted_terms = set(query_terms(title))
                current_terms = set(query_terms(current))
                if wanted_terms and wanted_terms.issubset(current_terms):
                    continue
                cur.execute('''
                    update roms
                    set name=%s, slug=%s, name_sort_key=%s, updated_at=current_timestamp()
                    where id=%s
                ''', (title, slugify_name(title), sort_key_for_name(title), row.get('id')))
                changed.append({'id': row.get('id'), 'old_name': current, 'new_name': title})
        con.commit()
    except Exception as e:
        print('romm title pin error', e, flush=True)
        return []
    finally:
        con.close()
    return changed

def organize_job(job):
    h=job['hash']
    if not h:
        # qBittorrent 5 can accept a URL/magnet as pending before the torrent hash is known.
        # Keep polling the ROM category and attach the hash once qBittorrent resolves it.
        try:
            arr=qbit_get('torrents/info', params={'category':QBIT_CATEGORY}).json()
            title=(job['title'] or '').lower()
            for t in sorted(arr, key=lambda x:x.get('added_on',0), reverse=True):
                name=(t.get('name') or '').lower()
                if title[:25] in name or name[:25] in title:
                    h=t.get('hash','')
                    if h:
                        qbit_post('torrents/setShareLimits', data={'hashes':h, 'ratioLimit':'-1', 'seedingTimeLimit':'-1', 'inactiveSeedingTimeLimit':'-1', 'shareLimitAction':'0'})
                        qbit_post('torrents/start', data={'hashes':h})
                        con=db(); con.execute('update jobs set hash=?, status=?, message=? where id=?',(h,'downloading','qBittorrent resolved the torrent hash; unlimited seeding is set.',job['id'])); con.commit(); con.close()
                    break
        except Exception as e:
            con=db(); con.execute('update jobs set status=?, message=? where id=?',('queued',f'Waiting for qBittorrent to resolve pending torrent: {e}',job['id'])); con.commit(); con.close()
        if not h:
            con=db(); con.execute('update jobs set status=?, message=? where id=?',('queued','Sent to qBittorrent; waiting for torrent metadata/hash to appear. If this stays here, pick a result with a healthier magnet/torrent source.',job['id'])); con.commit(); con.close()
            return
    info=qbit_get('torrents/info', params={'hashes':h}).json()
    if not info: return
    t=info[0]
    if float(t.get('progress',0)) < 1 or t.get('state') in ('downloading','stalledDL','metaDL','checkingDL','queuedDL'):
        pct=round(float(t.get('progress') or 0)*100, 1)
        con=db(); con.execute('update jobs set status=?, message=? where id=?',('downloading',f'qBittorrent: {pct}% · {t.get("state") or "active"}',job['id'])); con.commit(); con.close()
        return
    content=Path(t.get('content_path') or t.get('save_path') or '')
    if not content.exists():
        # qbit API returns container path; map to mounted /roms path when possible
        s=str(content)
        if s.startswith('/media/ptw/roms'):
            content=Path('/roms')/s[len('/media/ptw/roms/'):]
    detected_platform = job['platform'] or infer_platform(job['title'], str(content))
    prefer_dir_extract = detected_platform in ('Windows', 'Linux', 'DOS', 'Android')
    if content.is_dir():
        bundle = bundle_sources(content, detected_platform)
        files=[p for p in content.rglob('*') if p.is_file() and p.suffix.lower() in ROM_EXTS]
        if bundle:
            src=content
        elif files:
            content=max(files, key=lambda p:p.stat().st_size)
            src=extract_if_needed(content, prefer_dir=prefer_dir_extract)
        elif detected_platform in ('Xbox 360', 'Xbox', 'Xbox ONE', 'Windows', 'Linux', 'DOS'):
            src=content
        else:
            raise RuntimeError(f'No ROM-like files found in {content}')
    else:
        src=extract_if_needed(content, prefer_dir=prefer_dir_extract)
        detected_platform = detected_platform or infer_platform(job['title'], str(src))
    final_dir = romm_roms_dir(detected_platform)
    final_dir.mkdir(parents=True, exist_ok=True)
    import_plan = build_import_plan(src, detected_platform, final_dir, job['title'])
    if detected_platform and detected_platform != job['platform']:
        con=db(); con.execute('update jobs set platform=? where id=?',(detected_platform,job['id'])); con.commit(); con.close()
    stored_imports = []
    if job['imported_paths']:
        try:
            stored_imports = json.loads(job['imported_paths'])
        except Exception:
            stored_imports = []
    if job['status'] == 'verifying' and stored_imports:
        verified, missing = romm_verify_imports(stored_imports, detected_platform)
        final_path = stored_imports[0] if len(stored_imports) == 1 else str(final_dir)
        if not missing:
            pinned = romm_pin_single_import_title(stored_imports, detected_platform, job['title'])
            pin_note = f' Pinned RomM title to {pinned[0]["new_name"]}.' if pinned else ''
            con=db(); con.execute('update jobs set status=?, message=?, final_path=?, imported_paths=? where id=?',('complete',f'RomM import verified: {len(verified)} item(s) are visible in /roms/{detected_platform}/roms and qBittorrent source remains in _incoming for seeding.{pin_note}',final_path,json.dumps(stored_imports, ensure_ascii=False),job['id'])); con.commit(); con.close()
        else:
            waiting_on = ', '.join(Path(p).name for p in missing[:4])
            extra = f' Waiting on: {waiting_on}.' if waiting_on else ''
            con=db(); con.execute('update jobs set status=?, message=?, final_path=?, imported_paths=? where id=?',('verifying',f'Imported into /roms/{detected_platform}/roms and waiting for RomM autoscan to index {len(missing)}/{len(stored_imports)} remaining item(s).{extra}',final_path,json.dumps(stored_imports, ensure_ascii=False),job['id'])); con.commit(); con.close()
        return
    con=db(); con.execute('update jobs set status=?, message=? where id=?',('organizing','Download complete; hardlinking/copying into the real RomM console folder while keeping qBittorrent source in _incoming for seeding.',job['id'])); con.commit(); con.close()
    imported_paths, copy_notes = import_plan_into_romm(import_plan)
    copy_msg = summarize_import(copy_notes, detected_platform, len(imported_paths))
    verified, missing = romm_verify_imports(imported_paths, detected_platform)
    final_path = imported_paths[0] if len(imported_paths) == 1 else str(final_dir)
    stored_json = json.dumps(imported_paths, ensure_ascii=False)
    if missing:
        waiting_on = ', '.join(Path(p).name for p in missing[:4])
        extra = f' Waiting on: {waiting_on}.' if waiting_on else ''
        con=db(); con.execute('update jobs set status=?, message=?, final_path=?, imported_paths=? where id=?',('verifying',f'Payload imported into /roms/{detected_platform}/roms ({copy_msg}). Waiting for RomM autoscan to confirm {len(verified)}/{len(imported_paths)} item(s).{extra}',final_path,stored_json,job['id'])); con.commit(); con.close()
    else:
        pinned = romm_pin_single_import_title(imported_paths, detected_platform, job['title'])
        pin_note = f' Pinned RomM title to {pinned[0]["new_name"]}.' if pinned else ''
        con=db(); con.execute('update jobs set status=?, message=?, final_path=?, imported_paths=? where id=?',('complete',f'RomM import verified: payload is visible in /roms/{detected_platform}/roms ({copy_msg}). qBittorrent source remains in _incoming for seeding.{pin_note}',final_path,stored_json,job['id'])); con.commit(); con.close()

def worker():
    while True:
        try:
            con=db(); rows=con.execute("select * from jobs where status in ('queued','downloading','organizing','verifying') or (status='error' and hash is not null and hash != '') order by id desc limit 50").fetchall(); con.close()
            for j in rows:
                try:
                    organize_job(j)
                except Exception as e:
                    con=db(); con.execute('update jobs set status=?, message=? where id=?',('error',str(e),j['id'])); con.commit(); con.close()
        except Exception as e:
            print('worker error', e, flush=True)
        time.sleep(POLL_SECONDS)

@app.route('/thumb/<path:resource>')
def romm_thumb(resource):
    root = ROMM_RESOURCES_ROOT.resolve()
    rel = resource.lstrip('/').replace('..', '')
    path = (root / rel).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        return Response(placeholder_svg(Path(rel).stem or 'Game'), mimetype='image/svg+xml')
    return send_file(path, max_age=3600)

@app.route('/placeholder-cover')
def placeholder_cover():
    return Response(placeholder_svg(request.args.get('title', 'Game')), mimetype='image/svg+xml')

@app.route('/login', methods=['GET','POST'])
def login():
    nxt = request.values.get('next') or url_for('index')
    if request.method == 'POST':
        username = request.form.get('username','')
        password = request.form.get('password','')
        if login_valid(username, password):
            session['logged_in'] = True
            session['user'] = username
            return redirect(nxt if nxt.startswith('/') else url_for('index'))
        flash('Invalid login')
    return render_template_string(LOGIN_TPL, next=nxt)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@require_auth
def index():
    q=request.args.get('q','').strip(); platform=request.args.get('platform','')
    sort_by=normalize_sort_choice(request.args.get('sort','relevance'))
    results=None
    page=1
    total_pages=1
    total_results=0
    if q:
        try:
            page_param=request.args.get('page','1')
            page=int(page_param) if str(page_param).isdigit() else 1
            if page < 1:
                page = 1
            all_results=sort_search_results(prowlarr_search(q, platform), sort_by)
            total_results=len(all_results)
            total_pages=max(1, (len(all_results) + max(1, RESULTS_PER_PAGE) - 1)//max(1, RESULTS_PER_PAGE))
            if page > total_pages:
                page = total_pages
            start=(page-1)*max(1, RESULTS_PER_PAGE)
            end=start+max(1, RESULTS_PER_PAGE)
            results=public_search_results(all_results[start:end])
            pager_prev=build_search_url(q, platform, sort_by, page-1) if page>1 else None
            pager_next=build_search_url(q, platform, sort_by, page+1) if page < total_pages else None
            pager_pages=[(p, build_search_url(q, platform, sort_by, p)) for p in range(1, total_pages + 1)]
        except Exception as e:
            flash(f'Prowlarr search failed: {e}'); results=[]
            total_pages=1
            page=1
            total_results=0
            pager_prev=None
            pager_next=None
            pager_pages=[]
        
    else:
        pager_prev=None
        pager_next=None
        pager_pages=[]
    return render_template_string(
        TPL,
        q=q,
        platform=platform,
        platforms=platform_options(),
        results=results,
        total_results=total_results,
        sort_by=sort_by,
        sort_label=sort_label(sort_by),
        sort_options=SEARCH_SORT_OPTIONS,
        current_search_url=build_search_url(q, platform, sort_by, page) if q else url_for('index'),
        cat=QBIT_CATEGORY,
        romm=ROMM_URL,
        landing=landing_widgets(),
        page=page,
        total_pages=total_pages,
        pager_prev=pager_prev,
        pager_next=pager_next,
        pager_pages=pager_pages,
        results_per_page=max(1, RESULTS_PER_PAGE),
    )

@app.post('/add')
@require_auth
def add():
    title=request.form['title']; url=request.form['url']; platform=request.form.get('platform',''); indexer=request.form.get('indexer','')
    search_q = request.form.get('search_q', '')
    return_to = (request.form.get('return_to') or request.referrer or url_for('index')).strip()
    if not return_to.startswith('/'):
        return_to = url_for('index')
    try:
        result = add_torrent_with_fallback(url, title, platform, indexer, search_q=search_q)
        if result.get('used_fallback'):
            flash(f'Added via fallback result from {result.get("indexer") or "alternate source"}. Hash: {result.get("hash") or "pending"}')
        else:
            flash(f'Added to qBittorrent. Hash: {result.get("hash") or "pending"}')
    except Exception as e:
        flash(f'Add failed: {e}')
    return redirect(return_to)

@app.route('/jobs')
@require_auth
def jobs():
    con=db(); rows=con.execute('select * from jobs order by id desc limit 100').fetchall(); con.close()
    job_rows = enrich_jobs(rows)
    active_jobs = [j for j in job_rows if j.get('status') != 'complete']
    completed_jobs = [j for j in job_rows if j.get('status') == 'complete']
    return render_template_string(JOBS_TPL, jobs=job_rows, active_jobs=active_jobs, completed_jobs=completed_jobs, qbit_roms=qbit_rom_rows(), romm=ROMM_URL)

@app.post('/jobs/<int:job_id>/remove')
@require_auth
def remove_job(job_id):
    con=db(); row=con.execute('select * from jobs where id=?',(job_id,)).fetchone(); con.close()
    if not row:
        flash('Job not found')
        return redirect(url_for('jobs'))
    removed_torrent = False
    if row['hash']:
        try:
            requests.post(f'{QBIT_URL}/api/v2/torrents/delete', data={'hashes': row['hash'], 'deleteFiles': 'false'}, timeout=30).raise_for_status()
            removed_torrent = True
        except Exception as e:
            flash(f'Could not remove qBittorrent torrent for this job: {e}')
    con=db(); con.execute('delete from jobs where id=?',(job_id,)); con.commit(); con.close()
    flash('Job removed' + (' and qBittorrent torrent removed.' if removed_torrent else '.'))
    return redirect(url_for('jobs'))

@app.get('/api/native/search')
@require_auth
def api_native_search():
    q = (request.args.get('q') or '').strip()
    platform = request.args.get('platform', '')
    sort_by = normalize_sort_choice(request.args.get('sort', 'relevance'))
    page_param = request.args.get('page', '1')
    page = int(page_param) if str(page_param).isdigit() else 1
    page = max(1, page)
    if not q:
        return jsonify(ok=True, kind='games', query=q, page=1, total_pages=1, total_results=0, results=[], filters={'platforms': platform_options(), 'sorts': SEARCH_SORT_OPTIONS})
    try:
        all_results = sort_search_results(prowlarr_search(q, platform), sort_by)
        total_results = len(all_results)
        per_page = max(1, RESULTS_PER_PAGE)
        total_pages = max(1, (total_results + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        results = public_search_results(all_results[start:start + per_page])
        return jsonify(ok=True, kind='games', query=q, platform=platform, sort=sort_by, page=page, total_pages=total_pages, total_results=total_results, results=results, filters={'platforms': platform_options(), 'sorts': SEARCH_SORT_OPTIONS})
    except Exception as exc:
        return jsonify(ok=False, error=f'GameFinder search failed: {exc}', kind='games', query=q, results=[]), 502

@app.post('/api/native/request')
@require_auth
def api_native_request():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    url = (data.get('url') or '').strip()
    platform = (data.get('platform') or '').strip()
    indexer = (data.get('indexer') or '').strip()
    search_q = (data.get('search_q') or data.get('query') or '').strip()
    if not title or not url:
        return jsonify(ok=False, error='Missing title or download URL.'), 400
    try:
        result = add_torrent_with_fallback(url, title, platform, indexer, search_q=search_q)
        msg = 'Added to qBittorrent.'
        if result.get('used_fallback'):
            msg = f'Added via fallback result from {result.get("indexer") or "alternate source"}.'
        return jsonify(ok=True, kind='games', title=result.get('title') or title, hash=result.get('hash') or '', used_fallback=bool(result.get('used_fallback')), message=msg)
    except Exception as exc:
        return jsonify(ok=False, error=f'Add failed: {exc}'), 500

@app.route('/health')
def health():
    return jsonify(ok=True, prowlarr=PROWLARR_URL, qbit=QBIT_URL, downloaded_root=str(DOWNLOADED_ROOT))

@app.route('/admin/add-torrent', methods=['GET'])
@require_auth
def admin_add_torrent_form():
    return render_template_string(ADMIN_ADD_TPL, platforms=platform_options())

@app.post('/admin/add-torrent')
@require_auth
def admin_add_torrent_submit():
    title = (request.form.get('title') or '').strip()
    platform = (request.form.get('platform') or '').strip()
    save_path = (request.form.get('save_path') or '').strip()
    uploaded = request.files.get('torrent')
    if not title:
        flash('Title is required.')
        return redirect(url_for('admin_add_torrent_form'))
    if not uploaded or uploaded.filename == '':
        flash('Upload a .torrent file.')
        return redirect(url_for('admin_add_torrent_form'))
    data = {'category': QBIT_CATEGORY}
    if save_path:
        data['savepath'] = save_path
    else:
        data['savepath'] = QBIT_SAVE_PATH
    files = {'torrents': (safe_name(uploaded.filename), uploaded.read(), 'application/x-bittorrent')}
    before = {t.get('hash','').lower() for t in qbit_get('torrents/info', params={'category': QBIT_CATEGORY}).json() if t.get('hash')}
    resp = qbit_add(data=data, files=files)
    add_text = (resp.text or '').strip()
    h = ''
    last_arr = []
    for _ in range(12):
        time.sleep(1)
        arr = qbit_get('torrents/info', params={'category': QBIT_CATEGORY}).json()
        last_arr = arr
        new = [t for t in arr if t.get('hash','').lower() not in before]
        candidates = new or arr
        for t in sorted(candidates, key=lambda x: x.get('added_on', 0), reverse=True):
            name = (t.get('name') or '').lower()
            if new or title.lower()[:25] in name or name[:25] in title.lower():
                h = t.get('hash', '')
                break
        if h:
            break
    if not h and resp.status_code == 409:
        for t in sorted(last_arr, key=lambda x: x.get('added_on', 0), reverse=True):
            name = (t.get('name') or '').lower()
            if title.lower()[:25] in name or name[:25] in title.lower():
                h = t.get('hash', '')
                break
    if not h:
        flash(f'qBittorrent accepted the file but no torrent appeared in category {QBIT_CATEGORY}. Response: {add_text[:200] or "empty"}')
        return redirect(url_for('admin_add_torrent_form'))
    qbit_post('torrents/setShareLimits', data={'hashes': h, 'ratioLimit': '-1', 'seedingTimeLimit': '-1', 'inactiveSeedingTimeLimit': '-1', 'shareLimitAction': '0'})
    qbit_post('torrents/start', data={'hashes': h})
    con = db()
    con.execute('insert into jobs(title,platform,indexer,url,hash,status,message) values(?,?,?,?,?,?,?)', (title, platform, 'admin-upload', '', h, 'downloading', 'Uploaded .torrent and sent to qBittorrent.'))
    con.commit()
    con.close()
    flash(f'Uploaded and added to qBittorrent. Hash: {h}')
    return redirect(url_for('jobs'))

ADMIN_ADD_TPL = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="mobile-web-app-capable" content="yes"><meta name="theme-color" content="#10091a"><style>
:root{color-scheme:dark;--bg:#10091a;--bg2:#0d0714;--card:rgba(38,26,56,.86);--line:rgba(196,181,253,.16);--line2:rgba(196,181,253,.28);--text:#f5effb;--muted:#b6a8c7;--accent:#9f8ad7;--accent2:#c3a6e8;--deep:#6f55b5;--soft:#a78bfa}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(circle at top,#2b1b44 0,var(--bg) 46%,var(--bg2) 100%);color:var(--text);margin:0}.wrap{max-width:780px;margin:0 auto;padding:24px}.card{background:linear-gradient(145deg,var(--card),rgba(24,15,37,.96));border:1px solid var(--line);border-radius:16px;padding:18px;margin:14px 0;box-shadow:0 14px 36px #0007}input,select,button{font:inherit;border-radius:10px;border:1px solid var(--line2);background:rgba(24,15,37,.9);color:var(--text);padding:10px;min-height:44px;width:100%}button{cursor:pointer;background:linear-gradient(135deg,var(--deep),var(--accent));border-color:rgba(196,181,253,.34);font-weight:750}.top{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.muted{color:var(--muted)}.small{font-size:12px}
</style></head><body><div class="wrap">
<div class="top"><h1>Add Torrent</h1><a href="/">Back</a></div>
{% with messages = get_flashed_messages() %}{% if messages %}<div class="card">{% for m in messages %}<div>{{m}}</div>{% endfor %}</div>{% endif %}{% endwith %}
<div class="card">
  <form method="post" action="/admin/add-torrent" enctype="multipart/form-data" class="grid" style="display:grid;gap:10px">
   <input name="title" placeholder="Title" value="{{ request.form.get('title','') }}">
   <select name="platform">{% for val,label in platforms %}<option value="{{val}}" {% if request.form.get('platform','')==val %}selected{% endif %}>{{label}}</option>{% endfor %}<option value="">No platform / custom</option></select>
   <input name="save_path" placeholder="Save path" value="{{ request.form.get('save_path','') or '/media/ptw/roms/Downloaded ROMS/_incoming' }}">
   <input type="file" name="torrent" accept="*/*">
   <button type="submit">Upload and add to qBittorrent</button>
  </form>
</div>
<div class="card">
  <p class="muted small">This uploads the .torrent file directly to qBittorrent in category <b>{{cat}}</b>. If Save path is empty it uses the default ROM incoming folder. The job appears on <a href="/jobs">Jobs</a>.</p>
</div>
</div></body></html>
'''

if __name__ == '__main__':
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    DOWNLOADED_ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    app.run(host='0.0.0.0', port=APP_PORT)
