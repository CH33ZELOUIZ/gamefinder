import os, re, time, json, sqlite3, threading, subprocess, shutil
from pathlib import Path
from urllib.parse import quote

import requests
import pymysql
from flask import Flask, request, redirect, url_for, render_template_string, jsonify, flash, session, send_file, Response
from functools import wraps
import hmac

APP_PORT = int(os.getenv('PORT', '3020'))
PROWLARR_URL = os.getenv('PROWLARR_URL', 'http://prowlarr:9696').rstrip('/')
PROWLARR_API_KEY = os.getenv('PROWLARR_API_KEY', '')
QBIT_URL = os.getenv('QBIT_URL', 'http://qbittorrent:8080').rstrip('/')
QBIT_CATEGORY = os.getenv('QBIT_CATEGORY', 'roms')
QBIT_SAVE_PATH = os.getenv('QBIT_SAVE_PATH', '/downloads/roms/_incoming')
ROMS_ROOT = Path(os.getenv('ROMS_ROOT', '/roms'))
DOWNLOADED_ROOT = Path(os.getenv('DOWNLOADED_ROOT', '/roms/Downloaded ROMS'))
QBIT_CONTAINER_ROOT = os.getenv('QBIT_CONTAINER_ROOT', '').rstrip('/')
ROMM_RESOURCES_ROOT = Path(os.getenv('ROMM_RESOURCES_ROOT', '/romm_resources'))
DB_PATH = os.getenv('DB_PATH', '/data/gamefinder.db')
ROMM_DB_HOST = os.getenv('DB_HOST', '')
ROMM_DB_NAME = os.getenv('DB_NAME', 'romm')
ROMM_DB_USER = os.getenv('DB_USER', '')
ROMM_DB_PASSWORD = os.getenv('DB_PASSWD', '')
ROMM_URL = os.getenv('ROMM_URL', 'http://romm:8080').rstrip('/')
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '45'))
PROWLARR_SEARCH_TIMEOUT = float(os.getenv('PROWLARR_SEARCH_TIMEOUT', '15'))
# Keep GameFinder scoped to fast console/ROM indexers instead of asking every Prowlarr
# indexer (movies/TV/general indexers make searches slow and return junk). Override with
# repeated Prowlarr indexer IDs if the server's Prowlarr IDs change.
def csv_values(name, default=''):
    return tuple(v.strip() for v in os.getenv(name, default).split(',') if v.strip())

GAMEFINDER_INDEXER_IDS = csv_values('GAMEFINDER_INDEXER_IDS', '')
PROWLARR_CONSOLE_CATEGORIES = csv_values('PROWLARR_CONSOLE_CATEGORIES', '1000')
AUTH_USER = os.getenv('GAMEFINDER_USER', '')
AUTH_PASSWORD = os.getenv('GAMEFINDER_PASSWORD', '')

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'change-me-in-production')

def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if AUTH_USER and AUTH_PASSWORD and not session.get('logged_in'):
            return redirect(url_for('login', next=request.full_path if request.query_string else request.path))
        return fn(*args, **kwargs)
    return wrapper

PLATFORM_ALIASES = [
    ('Xbox 360', ['xbox 360', '[xbox 360]', 'xbox360', '360']),
    ('Xbox', ['original xbox', '[xbox]', ' xbox ']),
    ('Xbox ONE', ['xbox one', 'xboxone']),
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

ROM_EXTS = {'.nes','.smc','.sfc','.gb','.gbc','.gba','.nds','.dsi','.n64','.z64','.v64','.iso','.cue','.bin','.chd','.cso','.rvz','.wbfs','.gcm','.xex','.gen','.md','.sms','.gg','.32x','.pce','.a26','.a52','.a78','.j64','.lnx','.ngp','.ngc','.ws','.wsc','.zip','.7z'}
ARCHIVE_EXTS = {'.zip','.7z','.rar'}

STYLE = '''
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0b1020;color:#e8ecff;margin:0}a{color:#8ab4ff}.wrap{max-width:1280px;margin:0 auto;padding:24px}.card{background:#141b34;border:1px solid #263153;border-radius:16px;padding:18px;margin:14px 0;box-shadow:0 8px 24px #0005}input,select,button{font:inherit;border-radius:10px;border:1px solid #38456f;background:#0d1428;color:#f1f4ff;padding:10px}button{cursor:pointer;background:#335cff;border-color:#5575ff}.danger{background:#351a22}.muted{color:#aab3d4}.grid{display:grid;grid-template-columns:1fr 220px 120px;gap:10px}.result{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}.pill{display:inline-block;background:#263153;border-radius:999px;padding:3px 8px;margin:2px;font-size:12px}.ok{color:#7dffb2}.bad{color:#ff8a8a}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #263153;padding:8px;text-align:left;vertical-align:top}.small{font-size:12px}.top{display:flex;justify-content:space-between;gap:12px;align-items:center}.bar{height:10px;background:#0d1428;border:1px solid #38456f;border-radius:999px;overflow:hidden;min-width:160px}.bar>span{display:block;height:100%;background:linear-gradient(90deg,#335cff,#7dffb2)}.status{font-weight:700}.nowrap{white-space:nowrap}
.jobs-shell{display:grid;gap:16px}.job-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.stat{background:#0d1428;border:1px solid #263153;border-radius:14px;padding:12px}.stat b{display:block;font-size:22px}.section-head{display:flex;justify-content:space-between;align-items:end;gap:12px;margin-top:10px}.job-list{display:grid;gap:12px}.job-card{background:#111832;border:1px solid #263153;border-radius:16px;padding:14px;display:grid;gap:12px}.job-card.active{border-color:#5575ff;box-shadow:0 0 0 1px #335cff55}.job-card.complete{border-color:#285c45}.job-top{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:start}.job-title{font-weight:750;font-size:16px;line-height:1.25;overflow-wrap:anywhere}.job-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.tag{display:inline-flex;align-items:center;gap:4px;background:#263153;border:1px solid #38456f;border-radius:999px;padding:3px 8px;font-size:12px;color:#d8def7}.tag.good{background:#143323;border-color:#2c8a55;color:#a8ffd0}.tag.warn{background:#3a2c10;border-color:#936d21;color:#ffe0a0}.tag.live{background:#162b55;border-color:#335cff;color:#cfe0ff}.job-grid{display:grid;grid-template-columns:1.1fr 1.1fr .75fr;gap:12px}.panel{background:#0d1428;border:1px solid #263153;border-radius:12px;padding:10px}.label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#aab3d4;margin-bottom:6px}.pathbox{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.35;overflow-wrap:anywhere;word-break:break-word;max-height:62px;overflow:auto}.msg{font-size:12px;color:#c8d0ee;line-height:1.35;max-height:50px;overflow:auto}.remove-btn{background:#351a22;border-color:#8b4150;padding:8px 10px}.qbit-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}.qbit-card{background:#0d1428;border:1px solid #263153;border-radius:14px;padding:12px;display:grid;gap:8px}.qbit-name{font-weight:700;line-height:1.25;overflow-wrap:anywhere}.empty{border:1px dashed #38456f;border-radius:14px;padding:18px;text-align:center;color:#aab3d4}@media(max-width:900px){.job-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.job-grid{grid-template-columns:1fr}.job-top{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.wrap{padding:14px}}@media(max-width:560px){.job-summary{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
.home-widgets{display:grid;gap:16px;margin-top:16px}.widget-head{display:flex;justify-content:space-between;align-items:end;gap:12px}.widget-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}.game-tile{display:grid;grid-template-rows:210px auto;background:#111832;border:1px solid #263153;border-radius:16px;overflow:hidden;text-decoration:none;color:#e8ecff;box-shadow:0 8px 20px #0004}.game-tile:hover{border-color:#5575ff;transform:translateY(-1px)}.game-cover{width:100%;height:210px;object-fit:cover;background:#0d1428}.game-info{padding:10px;display:grid;gap:6px}.game-name{font-weight:750;line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.game-platform{font-size:12px;color:#aab3d4}.game-rating{font-size:12px;color:#7dffb2}.hero-strip{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.hero-panel{background:linear-gradient(135deg,#17224a,#10172f);border:1px solid #263153;border-radius:18px;padding:18px;min-height:160px}.quick-picks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.pick{background:#0d1428;border:1px solid #263153;border-radius:14px;padding:12px;text-decoration:none;color:#e8ecff}.pick b{display:block;margin-bottom:4px}@media(max-width:900px){.hero-strip{grid-template-columns:1fr}.widget-grid{grid-template-columns:repeat(auto-fill,minmax(135px,1fr))}.game-tile{grid-template-rows:180px auto}.game-cover{height:180px}.quick-picks{grid-template-columns:1fr}}
</style>
'''
TPL = STYLE + '''
<div class="wrap">
 <div class="top"><h1>GameFinder</h1><div><a href="/">Search</a> · <a href="/jobs">Jobs</a> · <a href="{{ romm }}">Open RomM</a> · <a href="/logout">Logout</a></div></div>
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
 {% if results is not none %}<div class="card"><h2>Results for {{q}}</h2>{% if not results %}<p>No results.</p>{% endif %}
  {% for r in results %}<div class="result card">
    <div><b>{{r.title}}</b><br><span class="muted">{{r.indexer}} · {{r.size_h}} · {{r.protocol}}</span><br>
      {% for c in r.categories %}<span class="pill">{{c}}</span>{% endfor %}</div>
    <form method="post" action="/add">
      <input type="hidden" name="title" value="{{r.title}}"><input type="hidden" name="url" value="{{r.url}}"><input type="hidden" name="platform" value="{{platform}}"><input type="hidden" name="indexer" value="{{r.indexer}}"><button>Add to Library</button>
    </form>
  </div>{% endfor %}</div>{% endif %}
</div>'''
JOBS_TPL = STYLE + '''<meta http-equiv="refresh" content="10">
<div class="wrap">
 <div class="top"><h1>GameFinder Jobs</h1><div><a href="/">Search</a> · <a href="{{ romm }}">Open RomM</a> · <a href="/logout">Logout</a></div></div>
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
    return '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="440" viewBox="0 0 320 440"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#17224a"/><stop offset="1" stop-color="#0d1428"/></linearGradient></defs><rect width="320" height="440" fill="url(#g)"/><rect x="22" y="22" width="276" height="396" rx="22" fill="none" stroke="#5575ff" stroke-width="3" opacity=".7"/><text x="160" y="205" text-anchor="middle" fill="#e8ecff" font-family="Arial,sans-serif" font-size="28" font-weight="700">GameFinder</text><text x="160" y="250" text-anchor="middle" fill="#aab3d4" font-family="Arial,sans-serif" font-size="18">' + safe + '</text></svg>'

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('''create table if not exists jobs(id integer primary key, created_at text default current_timestamp, title text, platform text, indexer text, url text, hash text, status text, message text, final_path text)''')
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
    'Nintendo 3DS': (1110,),
    'PlayStation Vita': (1120,),
    'Nintendo Wii U': (1130,),
    'Xbox ONE': (1140,),
    'PlayStation 4': (1180,),
}
PLATFORM_SEARCH_CATEGORY_IDS = PLATFORM_CATEGORY_IDS
CONSOLE_TITLE_TOKENS = ('nintendo','switch','nds','3ds','wii','gamecube','gba','gbc','game boy','gameboy','nes','snes','n64','playstation','ps1','ps2','ps3','ps4','psp','vita','xbox','dreamcast','saturn','sega','atari','rom')
BAD_CATEGORY_PREFIXES = ('movies', 'tv', 'audio', 'books', 'xxx')

def has_console_category(cats):
    names = [c.lower() for c in flatten_cats(cats)]
    ids = category_ids(cats)
    return any(n.startswith('console') for n in names) or any(1000 <= cid < 1200 for cid in ids)

def is_game_result(x):
    cats = flatten_cats(x.get('categories'))
    cat_l = [c.lower() for c in cats]
    if any(c.startswith(BAD_CATEGORY_PREFIXES) for c in cat_l):
        return False
    if has_console_category(x.get('categories')):
        return True
    # Last-resort safety net for indexers that return sparse categories, but only for
    # obvious console/ROM result titles. This avoids accepting Movies/TV/Audio junk.
    hay = ' '.join([x.get('title',''), x.get('indexer','')] + [c or '' for c in cats]).lower()
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
    ids = set(category_ids(x.get('categories')))
    wanted_ids = set(PLATFORM_CATEGORY_IDS.get(platform, ()))
    if wanted_ids and ids.intersection(wanted_ids):
        return True
    hay = ' '.join([x.get('title',''), x.get('indexer','')] + cats).lower()
    if platform.lower() in hay:
        return True
    return any(alias.lower() in hay for alias in platform_aliases(platform))

def search_category_ids(platform):
    # Use the broad console bucket even when a platform is selected. Prowlarr/indexer
    # category mappings are inconsistent; Xbox 360 releases often show up as Console,
    # PC/Games, or Other while still having explicit Xbox 360 title text.
    return PROWLARR_CONSOLE_CATEGORIES

def query_with_platform(q, platform):
    if not platform:
        return q
    q_l = q.lower()
    # Prefer human platform names in the Prowlarr query so indexers with coarse
    # categories still return platform-specific results.
    additions=[]
    for token in [platform] + platform_aliases(platform):
        token = token.strip('[] .')
        if token and token.lower() not in q_l and token.lower() not in additions:
            additions.append(token)
    return (q + ' ' + ' '.join(additions[:1])).strip()

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
    r=requests.get(f'{PROWLARR_URL}/api/v1/search', params=prowlarr_params(q, platform, categories), timeout=PROWLARR_SEARCH_TIMEOUT)
    r.raise_for_status()
    return r.json()

def prowlarr_search(q, platform=''):
    raw=[]
    seen=set()
    searches = [search_category_ids(platform)]
    # For a selected console, add a second tightly worded all-category search so
    # console files miscategorized as Other/PC-Games still show up, then filter hard.
    if platform:
        searches.append(())
    for categories in searches:
        for x in fetch_prowlarr(q, platform, categories):
            key=result_key(x)
            if key in seen:
                continue
            seen.add(key)
            raw.append(x)
    out=[]
    for x in raw[:240]:
        if not is_game_result(x) or not matches_platform(x, platform):
            continue
        url=x.get('magnetUrl') or x.get('downloadUrl')
        if not url: continue
        out.append({'title':x.get('title',''), 'indexer':x.get('indexer',''), 'size_h':human(x.get('size')), 'protocol':x.get('protocol',''), 'url':url, 'categories':flatten_cats(x.get('categories'))})
    return out

def qbit_post(path, data=None, files=None):
    r=requests.post(f'{QBIT_URL}/api/v2/{path}', data=data, files=files, timeout=30)
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
        resp = qbit_post('torrents/add', data={**data, 'urls':url})
    else:
        # Do not make qBittorrent fetch LAN/Prowlarr URLs from inside its VPN namespace.
        # Fetch the .torrent in GameFinder and upload the torrent bytes directly to qBit.
        # Some indexers/Prowlarr endpoints redirect to magnet: links; catch that redirect
        # and pass the magnet to qBit instead of letting requests try to fetch it.
        tr = requests.get(url, timeout=(15, 90), allow_redirects=False)
        if 300 <= tr.status_code < 400 and tr.headers.get('Location','').lower().startswith('magnet:'):
            resp = qbit_post('torrents/add', data={**data, 'urls':tr.headers['Location']})
        else:
            tr.raise_for_status()
            if not tr.content or len(tr.content) < 20:
                raise RuntimeError('Prowlarr returned an empty torrent file')
            files={'torrents': (safe_name(title)+'.torrent', tr.content, 'application/x-bittorrent')}
            resp = qbit_post('torrents/add', data=data, files=files)
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
    if not h:
        raise RuntimeError(f'qBittorrent accepted request but no ROM torrent appeared in category {QBIT_CATEGORY}. Response: {add_text[:200] or "empty"}')
    # Explicitly keep ROM jobs seeding indefinitely, regardless of future global/category changes.
    qbit_post('torrents/setShareLimits', data={'hashes':h, 'ratioLimit':'-1', 'seedingTimeLimit':'-1', 'inactiveSeedingTimeLimit':'-1', 'shareLimitAction':'0'})
    qbit_post('torrents/start', data={'hashes':h})
    con=db(); con.execute('insert into jobs(title,platform,indexer,url,hash,status,message) values(?,?,?,?,?,?,?)',(title,platform,indexer,url,h,'downloading','Sent to qBittorrent with unlimited seeding; waiting for download progress.')); con.commit(); con.close()
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
    if ext == '.nds' or '[nds]' in hay or ' nintendo ds' in hay or ' nds' in hay:
        return 'nds'
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

def extract_if_needed(path: Path):
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
    return max(files, key=lambda p:p.stat().st_size) if files else path

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
    con=db(); con.execute('update jobs set status=?, message=? where id=?',('organizing','Download complete; hardlinking/copying into the real RomM console folder while keeping qBittorrent source in _incoming for seeding.',job['id'])); con.commit(); con.close()
    content=Path(t.get('content_path') or t.get('save_path') or '')
    if not content.exists():
        # qbit API returns container path; map to mounted /roms path when possible
        s=str(content)
        if QBIT_CONTAINER_ROOT and s.startswith(QBIT_CONTAINER_ROOT + '/'):
            content = ROMS_ROOT / s[len(QBIT_CONTAINER_ROOT + '/'):]
    detected_platform = job['platform'] or infer_platform(job['title'], str(content))
    if content.is_dir():
        files=[p for p in content.rglob('*') if p.is_file() and p.suffix.lower() in ROM_EXTS]
        if files:
            content=max(files, key=lambda p:p.stat().st_size)
            src=extract_if_needed(content)
        elif detected_platform in ('Xbox 360', 'Xbox', 'Xbox ONE'):
            src=content
        else:
            raise RuntimeError(f'No ROM-like files found in {content}')
    else:
        src=extract_if_needed(content)
        detected_platform = detected_platform or infer_platform(job['title'], str(src))
    final_dir = romm_roms_dir(detected_platform)
    final_dir.mkdir(parents=True, exist_ok=True)
    final=final_dir/safe_name(src.name)
    final, copy_msg = keep_seeding_copy(src, final)
    # Keep the DB platform accurate after auto-detection.
    if detected_platform and detected_platform != job['platform']:
        con=db(); con.execute('update jobs set platform=? where id=?',(detected_platform,job['id'])); con.commit(); con.close()
    con=db(); con.execute('update jobs set status=?, message=?, final_path=? where id=?',('complete',f'RomM stage complete: payload is in /roms/{detected_platform}/roms ({copy_msg}). RomM filesystem watcher should auto-index it shortly; use Open RomM to confirm.',str(final),job['id'])); con.commit(); con.close()

def worker():
    while True:
        try:
            con=db(); rows=con.execute("select * from jobs where status in ('queued','downloading','organizing') or (status='error' and hash is not null and hash != '') order by id desc limit 50").fetchall(); con.close()
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
        if hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(password, AUTH_PASSWORD):
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
    results=None
    if q:
        try: results=prowlarr_search(q, platform)
        except Exception as e:
            flash(f'Prowlarr search failed: {e}'); results=[]
    return render_template_string(TPL, q=q, platform=platform, platforms=platform_options(), results=results, cat=QBIT_CATEGORY, romm=ROMM_URL, landing=landing_widgets())

@app.post('/add')
@require_auth
def add():
    title=request.form['title']; url=request.form['url']; platform=request.form.get('platform',''); indexer=request.form.get('indexer','')
    try:
        h=add_torrent(url,title,platform,indexer)
        flash(f'Added to qBittorrent. Hash: {h or "pending"}')
    except Exception as e:
        flash(f'Add failed: {e}')
    return redirect(url_for('jobs'))

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

@app.route('/health')
def health():
    return jsonify(ok=True, prowlarr=PROWLARR_URL, qbit=QBIT_URL, downloaded_root=str(DOWNLOADED_ROOT))

if __name__ == '__main__':
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    DOWNLOADED_ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    app.run(host='0.0.0.0', port=APP_PORT)
