import gzip
import os
import sec
import json
import queue
import re
import tarfile
import requests
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from collections import defaultdict
from io import BytesIO
from flask import Flask, render_template, request, flash, redirect, url_for, Response, session, abort, make_response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from lxml import etree

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_for_session')
default_db_uri = 'postgresql://xmltv:ballzXMLTVballz@192.168.1.198/xmltv'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', default_db_uri)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- Database Models ---

class Country(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False) # e.g., 'gb'
    name = db.Column(db.String(100), nullable=False)
    flag = db.Column(db.String(10))

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    tier = db.Column(db.String(20), default='free')  # 'free' or 'premium'
    api_key = db.Column(db.String(100), unique=True, nullable=False)
    premium_expiry = db.Column(db.DateTime, nullable=True)
    selected_channels = db.relationship('Channel', secondary='user_channels', backref=db.backref('users', lazy='dynamic'))

class ChannelGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    canonical_name = db.Column(db.String(200), nullable=False) # Name you want
    tvg_id = db.Column(db.String(100), nullable=False)         # Target tvg-id
    primary_channel_id = db.Column(db.String(100))             # Where to pull EPG from
    logo_override = db.Column(db.Text)                         # Group-level logo override
    channels = db.relationship('Channel', backref='group', lazy=True)

    @property
    def members_list(self):
        return [
            {"id": c.id, "name": c.name, "sources": ", ".join(c.all_source_names)}
            for c in self.channels
        ]

class GroupingJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_ids = db.Column(db.JSON, nullable=False) # List of IDs
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class BannedIP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(50), unique=True, nullable=False)
    reason = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

collection_channels = db.Table('collection_channels',
    db.Column('collection_id', db.Integer, db.ForeignKey('channel_collection.id'), primary_key=True),
    db.Column('channel_id', db.String(100), db.ForeignKey('channel.id'), primary_key=True)
)

class ChannelCollection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    channels = db.relationship('Channel', secondary=collection_channels, back_populates='collections')

channel_categories = db.Table('channel_categories',
    db.Column('channel_id', db.String(100), db.ForeignKey('channel.id'), primary_key=True),
    db.Column('category_id', db.Integer, db.ForeignKey('category.id'), primary_key=True)
)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    channels = db.relationship('Channel', secondary=channel_categories, back_populates='categories')

user_channels = db.Table('user_channels',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('channel_id', db.String(100), db.ForeignKey('channel.id'), primary_key=True)
)

class Source(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=True)
    url = db.Column(db.Text, nullable=False)
    country_id = db.Column(db.Integer, db.ForeignKey('country.id'), nullable=True)
    channel_count = db.Column(db.Integer, default=0)
    prog_count = db.Column(db.Integer, default=0)
    refresh_interval = db.Column(db.Integer, default=24)  # in hours
    next_update = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_updated = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_syncing = db.Column(db.Boolean, default=False)
    last_error = db.Column(db.Text, nullable=True)
    mappings = db.relationship('SourceChannelMapping', backref='source', lazy=True, cascade="all, delete-orphan")
    country = db.relationship('Country', backref='sources')

class SourceChannelMapping(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('source.id'), nullable=False)
    xml_cid = db.Column(db.String(100), nullable=False)
    canonical_cid = db.Column(db.String(100), db.ForeignKey('channel.id'), nullable=False)
    channel = db.relationship('Channel', backref=db.backref('mappings', lazy=True))

class Channel(db.Model):
    id = db.Column(db.String(100), primary_key=True)
    name = db.Column(db.String(200))
    name_norm = db.Column(db.String(200), index=True)
    group_id = db.Column(db.Integer, db.ForeignKey('channel_group.id'), nullable=True)
    country_id = db.Column(db.Integer, db.ForeignKey('country.id'), index=True)
    is_hidden = db.Column(db.Boolean, default=False)
    icon = db.Column(db.Text)
    logo_override = db.Column(db.Text)
    tvg_id_override = db.Column(db.String(100))
    name_override = db.Column(db.String(200))
    categories = db.relationship('Category', secondary=channel_categories, back_populates='channels')
    collections = db.relationship('ChannelCollection', secondary=collection_channels, back_populates='channels')
    programmes = db.relationship('Programme', backref='channel', lazy=True, cascade="all, delete-orphan")
    country = db.relationship('Country', backref=db.backref('channels', lazy='dynamic'))

    @property
    def all_source_names(self):
        """Returns unique source names for this channel and its group members."""
        names = set()
        target_channels = self.group.channels if self.group else [self]
        for ch in target_channels:
            for m in ch.mappings:
                names.add(m.source.name if m.source.name else f"#{m.source.id}")
        return sorted(list(names))

    @property
    def preferred_logo(self):
        """Returns the most appropriate logo: Group override > Channel override > Default icon."""
        if self.group and self.group.logo_override:
            return self.group.logo_override
        if self.logo_override:
            return self.logo_override
        return self.icon

class Programme(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.String(100), db.ForeignKey('channel.id'), nullable=False)
    start = db.Column(db.DateTime, index=True)
    stop = db.Column(db.DateTime)
    title = db.Column(db.Text)
    description = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint('channel_id', 'start', name='_channel_start_uc'),
    )

COUNTRIES = {
    'world': '🌎 World',
    'gb': '🇬🇧 United Kingdom', 'us': '🇺🇸 United States', 'ca': '🇨🇦 Canada', 'au': '🇦🇺 Australia',
    'ie': '🇮🇪 Ireland', 'de': '🇩🇪 Germany', 'fr': '🇫🇷 France', 'it': '🇮🇹 Italy', 'es': '🇪🇸 Spain',
    'nl': '🇳🇱 Netherlands', 'be': '🇧🇪 Belgium', 'ch': '🇨🇭 Switzerland', 'se': '🇸🇪 Sweden', 'no': '🇳🇴 Norway',
    'dk': '🇩🇰 Denmark', 'fi': '🇫🇮 Finland', 'pl': '🇵🇱 Poland', 'pt': '🇵🇹 Portugal', 'gr': '🇬🇷 Greece',
    'tr': '🇹🇷 Turkey', 'ru': '🇷🇺 Russia', 'in': '🇮🇳 India', 'pk': '🇵🇰 Pakistan', 'za': '🇿🇦 South Africa',
    'mx': '🇲🇽 Mexico', 'br': '🇧🇷 Brazil', 'ar': '🇦🇷 Argentina', 'co': '🇨🇴 Colombia', 'ng': '🇳🇬 Nigeria',
    'eg': '🇪🇬 Egypt', 'ae': '🇦🇪 UAE', 'sa': '🇸🇦 Saudi Arabia', 'il': '🇮🇱 Israel', 'jp': '🇯🇵 Japan',
    'kr': '🇰🇷 Korea', 'cn': '🇨🇳 China', 'hk': '🇭🇰 Hong Kong', 'sg': '🇸🇬 Singapore', 'my': '🇲🇾 Malaysia',
    'th': '🇹🇭 Thailand', 'id': '🇮🇩 Indonesia', 'ph': '🇵🇭 Philippines', 'vn': '🇻🇳 Vietnam', 'af': '🇦🇫 Afghanistan',
    'al': '🇦🇱 Albania', 'dz': '🇩🇿 Algeria', 'ad': '🇦🇩 Andorra', 'ao': '🇦🇴 Angola', 'am': '🇦🇲 Armenia',
    'ba': '🇧🇦 Bosnia', 'bg': '🇧🇬 Bulgaria', 'cl': '🇨🇱 Chile', 'cu': '🇨🇺 Cuba', 'cy': '🇨🇾 Cyprus',
    'cz': '🇨🇿 Czechia', 'ee': '🇪🇪 Estonia', 'et': '🇪🇹 Ethiopia', 'ge': '🇬🇪 Georgia', 'gh': '🇬🇭 Ghana',
    'gt': '🇬🇹 Guatemala', 'hu': '🇭🇺 Hungary', 'is': '🇮🇸 Iceland', 'iq': '🇮🇶 Iraq', 'jm': '🇯🇲 Jamaica',
    'jo': '🇯🇴 Jordan', 'ke': '🇰🇪 Kenya', 'kw': '🇰🇼 Kuwait', 'lv': '🇱🇻 Latvia', 'lb': '🇱🇧 Lebanon',
    'lt': '🇱🇹 Lithuania', 'lu': '🇱🇺 Luxembourg', 'ma': '🇲🇦 Morocco', 'nz': '🇳🇿 New Zealand', 'pa': '🇵🇦 Panama',
    'py': '🇵🇾 Paraguay', 'pe': '🇵🇪 Peru', 'qa': '🇶🇦 Qatar', 'ro': '🇷🇴 Romania', 'rs': '🇷🇸 Serbia',
    'sk': '🇸🇰 Slovakia', 'si': '🇸🇮 Slovenia', 'lk': '🇱🇰 Sri Lanka', 'sy': '🇸🇾 Syria', 'tw': '🇹🇼 Taiwan',
    'tn': '🇹🇳 Tunisia', 'ua': '🇺🇦 Ukraine', 'uy': '🇺🇾 Uruguay', 'uz': '🇺🇿 Uzbekistan', 've': '🇻🇪 Venezuela'
}

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(100))

# --- Auth Helpers ---

def get_current_user():
    user_id = session.get('user_id')
    if user_id:
        return db.session.get(User, user_id)
    return None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_id') is None:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not user.is_admin:
            flash("Admin access required.")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def get_setting(key, default=None):
    setting = Setting.query.filter_by(key=key).first()
    return setting.value if setting else default

def format_xmltv_date(date_obj_or_str):
    """Forces dates into the strict XMLTV UTC format (+0000)"""
    if not date_obj_or_str: return None
    try:
        # If it's already a datetime object from your DB:
        if isinstance(date_obj_or_str, datetime):
            return date_obj_or_str.strftime("%Y%m%d%H%M%S +0000")
        # If it's a string from your DB (YYYYMMDDHHMMSS):
        dt = datetime.strptime(str(date_obj_or_str)[:14], "%Y%m%d%H%M%S")
        return dt.strftime("%Y%m%d%H%M%S +0000")
    except:
        return str(date_obj_or_str) + " +0000"

# --- EPG Processing Logic ---

def normalize_name(name):
    """Standardizes channel names to identify duplicates (e.g. 'SkySp News HD' -> 'skyspnews')."""
    if not name: return ""
    name = name.lower()
    # Remove quality tags and region codes
    name = re.sub(r'\b(hd|sd|fhd|uhd|4k)\b', '', name)
    name = re.sub(r'\.(uk|it|fr|es|de|ie|com)$', '', name)
    # Remove anything inside brackets
    name = re.sub(r'\(.*?\)', '', name)
    # Remove all non-alphanumeric chars
    name = re.sub(r'[^a-z0-9]', '', name)
    return name

def parse_xmltv_date(date_str):
    """Parses XMLTV date strings into datetime objects using standard library."""
    if not date_str:
        return None
    try:
        # Standardize: handle 'Z', and colons in offsets (+01:00 or =03:00 -> +0100)
        clean_date = date_str.strip()
        if clean_date.endswith('Z'):
            clean_date = clean_date[:-1] + ' +0000'
        
        # Handle offsets starting with +, -, or = and remove colons
        clean_date = re.sub(r'([+=-]\d{2}):(\d{2})$', r' \1\2', clean_date)
        # Normalize '=' to '+' for strptime compatibility
        clean_date = clean_date.replace('=', '+')
        
        # Ensure space before offset if missing (e.g. 20240520200000+0100)
        if ' ' not in clean_date and any(c in clean_date for c in '+-'):
            clean_date = re.sub(r'([+-])', r' \1', clean_date)
        
        # Try common XMLTV date patterns with offsets
        for fmt in ["%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z", "%Y%m%d %z"]:
            try:
                dt = datetime.strptime(clean_date, fmt)
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                continue

        # Fallback for naive strings (no offset) - assume they are UTC
        return datetime.strptime(clean_date[:14], "%Y%m%d%H%M%S")
    except Exception:
        return None

def fetch_and_parse(url):
    """Downloads and extracts XML content based on file extension."""
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.content
        if url.endswith('.tar.gz'):
            with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if m.isfile() and m.name.endswith('.xml'):
                        return tar.extractfile(m).read()
        elif url.endswith('.xml.gz'):
            try: return gzip.decompress(data)
            except: return data
        return data
    except Exception as e:
        print(f"Fetch error: {e}")
        return None

CHUNK_SIZE = 500  # programmes committed per DB transaction

def sync_source(source, updated_canonical_ids):
    """
    Memory-efficient streaming parser using iterparse.
    Instead of loading the entire XML tree (~18 GB RAM for huge files),
    we process elements one-by-one and commit in chunks of CHUNK_SIZE.
    Progress is reported live into sync_progress[source.id].
    """
    sid = source.id
    sync_progress[sid] = {'phase': 'Downloading', 'channels': 0, 'progs': 0, 'done': False, 'error': None}

    content = fetch_and_parse(source.url)
    if not content:
        msg = "Fetch error: Could not download or decompress XML."
        source.last_error = msg
        db.session.commit()
        sync_progress[sid].update({'done': True, 'error': msg})
        return False

    try:
        sync_progress[sid]['phase'] = 'Parsing channels'

        # Pre-fetch existing data to avoid thousands of individual DB hits
        all_channels = {c.id: c for c in Channel.query.all()}
        known_mappings = {m.xml_cid: m.canonical_cid for m in source.mappings}

        cid_map = {}
        channels_synced = 0
        new_channels_buffer = []
        new_mappings_buffer = []

        ctx = etree.iterparse(BytesIO(content), events=('end',), tag='channel',
                              recover=True)
        for _, c_el in ctx:
            xml_cid = c_el.get('id')
            if not xml_cid: continue

            name = (c_el.xpath('display-name/text()') or [xml_cid])[0]
            icon_list = c_el.xpath('icon/@src')
            icon_url = icon_list[0] if icon_list else None

            target_channel = None
            # Check if this specific XML ID is already mapped for this source
            if xml_cid in known_mappings:
                target_channel = all_channels.get(known_mappings[xml_cid])
            
            # Check if a channel with this ID exists globally (e.g. from another EPG)
            if not target_channel:
                target_channel = all_channels.get(xml_cid)

            if not target_channel:
                norm = normalize_name(name)
                target_channel = Channel(
                    id=xml_cid, name=name, name_norm=norm, icon=icon_url,
                    country_id=source.country_id # Channel inherits source country
                )
                all_channels[xml_cid] = target_channel # Cache locally immediately
                new_channels_buffer.append(target_channel)
            elif target_channel.country_id is None and source.country_id is not None:
                # Backfill country_id for existing channels imported before country tracking
                target_channel.country_id = source.country_id

            # Create the mapping if it doesn't exist for this source
            if xml_cid not in known_mappings:
                new_mappings_buffer.append(SourceChannelMapping(
                    source_id=sid, xml_cid=xml_cid, canonical_cid=target_channel.id))
                known_mappings[xml_cid] = target_channel.id

            # Store programs under the individual channel ID to allow switching group masters easily
            cid_map[xml_cid] = target_channel.id

            channels_synced += 1
            sync_progress[sid]['channels'] = channels_synced

            c_el.clear()
            while c_el.getprevious() is not None:
                del c_el.getparent()[0]

        # Bulk save new channels and mappings to DB
        if new_channels_buffer:
            db.session.bulk_save_objects(new_channels_buffer)
        if new_mappings_buffer:
            db.session.bulk_save_objects(new_mappings_buffer)

        # Point 6: Overwrite strategy. Identify canonical channels provided by this source.
        target_cids = list(set(cid_map.values()))
        
        # Purge existing data for these channels if they haven't been synced yet in this session
        cids_to_purge = [cid for cid in target_cids if cid not in updated_canonical_ids]
        if cids_to_purge:
            Programme.query.filter(Programme.channel_id.in_(cids_to_purge)).delete(synchronize_session=False)

        # Pre-fetch existing starts to handle internal XML duplicates
        existing_progs = db.session.query(Programme.channel_id, Programme.start).filter(
            Programme.channel_id.in_(target_cids)).all()
        prog_cache = set((p.channel_id, p.start) for p in existing_progs)

        db.session.commit()

        # ---- Pass 2: stream programmes, commit every CHUNK_SIZE rows ----
        sync_progress[sid]['phase'] = 'Importing programmes'
        progs_synced = 0
        chunk_buffer = []

        ctx2 = etree.iterparse(BytesIO(content), events=('end',), tag='programme',
                               recover=True)
        for _, p_el in ctx2:
            xml_cid = p_el.get('channel')
            target_cid = cid_map.get(xml_cid)
            chan_obj = all_channels.get(target_cid)

            if not target_cid or target_cid in updated_canonical_ids or (chan_obj and chan_obj.is_hidden):
                p_el.clear()
                continue

            start_dt = parse_xmltv_date(p_el.get('start'))
            stop_dt = parse_xmltv_date(p_el.get('stop'))
            if not start_dt:
                p_el.clear()
                continue

            title = (p_el.xpath('title/text()') or ["No Title"])[0]
            desc = (p_el.xpath('desc/text()') or [""])[0]

            # High-speed duplicate guard using local cache instead of DB hits
            if (target_cid, start_dt) not in prog_cache:
                new_p = Programme(channel_id=target_cid, start=start_dt, stop=stop_dt, title=title, description=desc)
                chunk_buffer.append(new_p)
                prog_cache.add((target_cid, start_dt))
                progs_synced += 1

            p_el.clear()
            while p_el.getprevious() is not None:
                del p_el.getparent()[0]

            # Commit in chunks to keep memory low
            if len(chunk_buffer) >= CHUNK_SIZE:
                db.session.bulk_save_objects(chunk_buffer)
                db.session.commit()
                chunk_buffer = []
                sync_progress[sid]['progs'] = progs_synced

        # Final flush
        if chunk_buffer:
            db.session.bulk_save_objects(chunk_buffer)
            db.session.commit()

        # Mark all touched canonical IDs as done for this run
        for target_cid in cid_map.values():
            updated_canonical_ids.add(target_cid)

        source.channel_count = channels_synced
        source.prog_count = progs_synced
        source.last_updated = datetime.now(timezone.utc)
        source.next_update = datetime.now(timezone.utc) + timedelta(hours=source.refresh_interval)
        source.last_error = None
        db.session.commit()

        sync_progress[sid].update({'progs': progs_synced, 'done': True, 'phase': 'Complete', 'channel_count': channels_synced, 'prog_count': progs_synced})
        return True

    except Exception as e:
        db.session.rollback()
        source.last_error = str(e)
        db.session.commit()
        sync_progress[sid].update({'done': True, 'error': str(e), 'phase': 'Error'})
        print(f"Parse error for source {sid}: {e}")
        return False

def perform_source_deletion(source_id):
    """Helper to perform deep cleanup of a source in the background."""
    try:
        sync_progress[source_id] = {'phase': 'Identifying orphan channels', 'done': False, 'error': None}
        
        source = db.session.get(Source, source_id)
        if not source:
            sync_progress[source_id].update({'done': True, 'phase': 'Deleted'})
            return

        # 1. Identify channels that are only mapped to THIS source
        mappings = SourceChannelMapping.query.filter_by(source_id=source_id).all()
        canonical_ids = [m.canonical_cid for m in mappings]

        orphans = []
        for cid in canonical_ids:
            other_ref = SourceChannelMapping.query.filter(
                SourceChannelMapping.canonical_cid == cid,
                SourceChannelMapping.source_id != source_id
            ).first()
            if not other_ref:
                orphans.append(cid)

        # 2. Delete EPG Data for orphans (The heavy lifting)
        if orphans:
            sync_progress[source_id]['phase'] = f'Deleting EPG for {len(orphans)} channels'
            # Remove orphans from user selections
            db.session.execute(db.delete(user_channels).where(user_channels.c.channel_id.in_(orphans)))
            # Remove programme rows
            Programme.query.filter(Programme.channel_id.in_(orphans)).delete(synchronize_session=False)
            db.session.commit()

        # 3. Remove the Source record itself (cascades to mappings)
        # This MUST happen before removing orphan channels to clear FK references.
        sync_progress[source_id]['phase'] = 'Finalizing deletion'
        db.session.delete(source)
        db.session.commit()

        # 4. Safely remove orphan Channel records (FK references in mappings are gone)
        if orphans:
            sync_progress[source_id]['phase'] = 'Cleaning up orphan channels'
            Channel.query.filter(Channel.id.in_(orphans)).delete(synchronize_session=False)
            db.session.commit()

        # 5. Dissolve groups that now have < 2 members
        sync_progress[source_id]['phase'] = 'Validating channel groups'
        all_groups = ChannelGroup.query.all()
        for g in all_groups:
            count = db.session.query(db.func.count(Channel.id)).filter(Channel.group_id == g.id).scalar()
            if count < 2:
                # Remove the group association from the remaining channel (if any)
                Channel.query.filter(Channel.group_id == g.id).update({Channel.group_id: None}, synchronize_session=False)
                db.session.delete(g)
        db.session.commit()

        sync_progress[source_id].update({'done': True, 'phase': 'Deleted'})
    except Exception as e:
        db.session.rollback()
        print(f"Deletion error for source {source_id}: {e}")
        source = db.session.get(Source, source_id)
        if source:
            source.is_syncing = False
            source.last_error = f"Deletion failed: {e}"
            db.session.commit()
        sync_progress[source_id].update({'done': True, 'error': str(e), 'phase': 'Error'})

def generate_xmltv_content(channels):
    """Generates an XMLTV structure from a list of channels."""
    root = etree.Element("tv")
    processed_groups = set()

    for c in channels:
        if c.group_id:
            if c.group_id in processed_groups:
                continue
            processed_groups.add(c.group_id)
            
            group = db.session.get(ChannelGroup, c.group_id)
            if not group:
                continue
            
            display_id = group.tvg_id
            display_name = group.canonical_name
            
            c_el = etree.SubElement(root, "channel", id=display_id)
            etree.SubElement(c_el, "display-name").text = display_name
            
            # Pull programs from the primary channel
            primary = db.session.get(Channel, group.primary_channel_id)
            icon_to_use = primary.preferred_logo if primary else group.logo_override
            target_channels = [primary] if primary else []
        else:
            display_id = c.tvg_id_override if c.tvg_id_override else c.id
            display_name = c.name_override if c.name_override else c.name
            c_el = etree.SubElement(root, "channel", id=display_id)
            etree.SubElement(c_el, "display-name").text = display_name
            icon_to_use = c.preferred_logo
            target_channels = [c]

        if icon_to_use: 
            etree.SubElement(c_el, "icon", src=icon_to_use)

        for ch_obj in target_channels:
            for p in ch_obj.programmes:
                p_el = etree.SubElement(root, "programme", channel=display_id, 
                                       start=format_xmltv_date(p.start), 
                                       stop=format_xmltv_date(p.stop))
                etree.SubElement(p_el, "title").text = p.title
                etree.SubElement(p_el, "desc").text = p.description
    return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding='UTF-8')

# --- Routes ---

@app.route("/")
def index():
    return render_template('index.html', user=get_current_user(), registration_enabled=get_setting('registration_enabled', 'true') == 'true')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if get_setting('registration_enabled', 'true') != 'true':
        flash("Registration is currently disabled.")
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash("Username already exists.")
        else:
            new_user = User(username=username, password_hash=generate_password_hash(password), api_key=secrets.token_hex(16))
            db.session.add(new_user)
            db.session.commit()
            flash("Registration successful. Please log in.")
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    user = get_current_user()
    page = request.args.get('page', 1, type=int)
    country_id = request.args.get('country_id', type=int)
    category_id = request.args.get('cat_id', type=int)
    per_page = request.args.get('per_page', 100, type=int)
    search_q = request.args.get('q', '')
    domain_name = get_setting('domain_name', '')


    free_limit = int(get_setting('free_tier_limit', '200'))
    premium_limit = int(get_setting('premium_tier_limit', '500'))
    contributor_limit = int(get_setting('contributor_tier_limit', '1000'))
    sponsor_limit = int(get_setting('sponsor_tier_limit', '99999'))

    if user.tier == 'sponsor': limit = sponsor_limit
    elif user.tier == 'contributor': limit = contributor_limit
    elif user.tier == 'premium': limit = premium_limit
    else: limit = free_limit

    db.session.expire_all() # Ensure we see latest counts/sync states

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        post_country_id = request.form.get('country_id', type=int) or country_id

        # Base query for identifying "filtered" channels
        base_query = Channel.query.filter(
            db.and_(Channel.is_hidden == False, db.or_(Channel.group_id == None, Channel.id.in_(db.session.query(ChannelGroup.primary_channel_id))))
        )
        if post_country_id:
            base_query = base_query.filter(Channel.country_id == post_country_id)
        if category_id:
            base_query = base_query.filter(Channel.categories.any(Category.id == category_id))
        if search_q:
            base_query = base_query.filter(db.or_(Channel.name.ilike(f"%{search_q}%"), Channel.id.ilike(f"%{search_q}%")))

        current_selected_ids = set(c.id for c in user.selected_channels)

        if action == 'add_collection':
            coll = db.session.get(ChannelCollection, request.form.get('collection_id'))
            if coll:
                coll_ids = set(c.id for c in coll.channels)
                final_ids = list(current_selected_ids | coll_ids)[:limit]
                user.selected_channels = db.session.query(Channel).filter(Channel.id.in_(final_ids)).all()
                db.session.commit()
                flash(f"Collection '{coll.name}' added (capped at {limit} total).")
            return redirect(url_for('dashboard', country_id=post_country_id, q=search_q))

        # Standard per-page save logic
        submitted_ids = set(request.form.getlist('channels'))
        visible_channels = base_query.order_by(Channel.name).paginate(page=page, per_page=per_page, error_out=False).items
        visible_ids = set(c.id for c in visible_channels)

        # Merge selections: keep selections from other pages, update selections for current page
        final_selection_ids = list((current_selected_ids - visible_ids) | submitted_ids)

        if len(final_selection_ids) > limit:
            final_selection_ids = final_selection_ids[:limit]
            flash(f"Channel limit reached. Selection capped at {limit}.")

        user.selected_channels = db.session.query(Channel).filter(Channel.id.in_(final_selection_ids)).all()
        db.session.commit()
        flash("Channel selection updated.")
        return redirect(url_for('dashboard', country_id=post_country_id, page=page, per_page=per_page, q=search_q))
    
    query = Channel.query.options(db.joinedload(Channel.group)).filter(
        db.and_(Channel.is_hidden == False, db.or_(Channel.group_id == None, Channel.id.in_(db.session.query(ChannelGroup.primary_channel_id))))
    )

    if country_id:
        query = query.filter(Channel.country_id == country_id)

    if category_id:
        query = query.filter(Channel.categories.any(Category.id == category_id))

    if search_q:
        query = query.filter(db.or_(
            Channel.name.ilike(f"%{search_q}%"),
            Channel.id.ilike(f"%{search_q}%")
        ))

    query = query.order_by(Channel.name)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    channels = pagination.items

    # Fetch only countries that actually have active sources (distinct to avoid duplicates)
    active_countries = Country.query.filter(Country.channels.any()).order_by(Country.name).all()

    collections = ChannelCollection.query.order_by(ChannelCollection.name).all()
    categories = Category.query.order_by(Category.name).all()

    selected_ids = [c.id for c in user.selected_channels]
    return render_template('dashboard.html', user=user, channels=channels, selected_ids=selected_ids, limit=limit, 
                           pagination=pagination, countries=active_countries, country_id=country_id, category_id=category_id, per_page=per_page,
                           search_q=search_q, domain_name=domain_name, categories=categories,
                           collections=collections, free_limit=free_limit, premium_limit=premium_limit, 
                           contributor_limit=contributor_limit, sponsor_limit=sponsor_limit)

@app.route('/dashboard/password', methods=['POST'])
@login_required
def change_password():
    user = get_current_user()
    new_pass = request.form.get('new_password')
    confirm_pass = request.form.get('confirm_password')

    if not new_pass or new_pass != confirm_pass:
        flash("Passwords do not match or are empty.")
        return redirect(url_for('dashboard'))

    user.password_hash = generate_password_hash(new_pass)
    db.session.commit()
    flash("Password updated successfully.")
    return redirect(url_for('dashboard'))

@app.route('/guide')
def guide():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user = get_current_user()

    if user and user.selected_channels:
        # Ensure we only show one representative per group from the user's selections
        channels = []
        seen_groups = set()
        for c in user.selected_channels:
            if not c.group_id:
                channels.append(c)
            elif c.group_id not in seen_groups:
                # Fetch the primary channel for this group
                primary = db.session.get(Channel, c.group.primary_channel_id)
                if primary: channels.append(primary)
                seen_groups.add(c.group_id)
        channels.sort(key=lambda c: c.name)
    else:
        # FIX: Show nothing if no channels selected
        channels = []

    channel_ids = [c.id for c in channels]

    # Get timeline boundaries to align all channels
    first_prog = Programme.query.filter(Programme.channel_id.in_(channel_ids)).order_by(Programme.start).first() if channel_ids else None
    last_prog = Programme.query.filter(Programme.channel_id.in_(channel_ids)).order_by(Programme.stop.desc()).first() if channel_ids else None

    def to_naive(dt):
        if not dt: return None
        if dt.tzinfo:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    t_start = to_naive(first_prog.start) if first_prog else now
    t_end = to_naive(last_prog.stop) if last_prog else now
    total_width = ((t_end - t_start).total_seconds() / 60) * 5

    return render_template('select.html', user=user, channels=channels, now=now, timeline_start=t_start, total_width=total_width, to_naive=to_naive)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and not user.is_active:
            flash('Account disabled by admin.')
            return render_template('login.html')
        elif user and check_password_hash(user.password_hash, request.form['password']):
            session['user_id'] = user.id
            return redirect(url_for('guide'))
        flash('Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    return redirect(url_for('index'))

@app.route('/admin')
@login_required
@admin_required
def admin():
    db.session.expire_all() # Ensure we see latest sync states and stats
    sources = Source.query.all()
    users = User.query.all()
    page = request.args.get('page', 1, type=int)
    country_id = request.args.get('country_id', type=int)
    category_id = request.args.get('cat_id', type=int)
    per_page = request.args.get('per_page', 100, type=int)
    search_q = request.args.get('q', '')

    query = Channel.query.options(db.joinedload(Channel.group)).filter(
        db.and_(
            Channel.is_hidden == False, 
            db.or_(Channel.group_id == None, Channel.id.in_(db.session.query(ChannelGroup.primary_channel_id)))
        )
    )

    if country_id:
        query = query.filter(Channel.country_id == country_id)

    if category_id:
        query = query.filter(Channel.categories.any(Category.id == category_id))

    if search_q:
        query = query.filter(db.or_(
            Channel.name.ilike(f"%{search_q}%"),
            Channel.id.ilike(f"%{search_q}%")
        ))

    query = query.order_by(Channel.name)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    channels = pagination.items

    hidden_channels = Channel.query.filter_by(is_hidden=True).order_by(Channel.name).all()
    
    # All countries for the "Add Source" dropdown
    all_countries = Country.query.order_by(Country.name).all()
    # Only countries with existing sources for the filter (distinct to avoid duplicates when a country has multiple sources)
    active_countries = Country.query.filter(Country.channels.any()).order_by(Country.name).all()
    collections = ChannelCollection.query.order_by(ChannelCollection.name).all()
    categories = Category.query.order_by(Category.name).all()

    # Statistics for the new Stats tab
    stats = {
        'total_channels': Channel.query.count(),
        'total_programmes': Programme.query.count(),
        'total_hidden': Channel.query.filter_by(is_hidden=True).count(),
        'total_groups': ChannelGroup.query.count(),
        'country_counts': db.session.query(Country, db.func.count(Channel.id))
            .join(Channel)
            .group_by(Country.id)
            .order_by(db.func.count(Channel.id).desc())
            .all()
    }

    reg_enabled = get_setting('registration_enabled', 'true')
    free_limit = get_setting('free_tier_limit', '200')
    premium_limit = get_setting('premium_tier_limit', '500')
    contributor_limit = get_setting('contributor_tier_limit', '1000')
    sponsor_limit = get_setting('sponsor_tier_limit', '99999')
    retention_days = get_setting('epg_retention_days', '2')

    domain_name = get_setting('domain_name', '')
    github_token = get_setting('github_token', '')

    banned_ips = BannedIP.query.order_by(BannedIP.created_at.desc()).all()
    group_jobs = GroupingJob.query.order_by(GroupingJob.created_at.asc()).all()

    return render_template('admin.html', 
                           user=get_current_user(),
                           sources=sources, users=users, channels=channels, pagination=pagination, hidden_channels=hidden_channels,
                           countries=active_countries, all_countries=all_countries, collections=collections, categories=categories, country_id=country_id, category_id=category_id, per_page=per_page,
                           reg_enabled=reg_enabled, free_limit=free_limit, premium_limit=premium_limit,
                           contributor_limit=contributor_limit, sponsor_limit=sponsor_limit,
                           stats=stats,
                           retention_days=retention_days,
                           search_q=search_q, domain_name=domain_name,
                           github_token=github_token, banned_ips=banned_ips, group_jobs=group_jobs)

@app.route('/admin/security/unban/<int:ban_id>', methods=['POST'])
@login_required
@admin_required
def admin_unban_ip(ban_id):
    ban = db.session.get(BannedIP, ban_id)
    if ban:
        ip = ban.ip
        db.session.delete(ban)
        db.session.commit()
        flash(f"IP {ip} has been unbanned.")
    return redirect(url_for('admin') + "#security")

@app.route('/admin/channel/epg_preview')
@login_required
@admin_required
def admin_epg_preview():
    """Returns a JSON preview of upcoming programs for multiple channels."""
    cids = request.args.getlist('ids')
    if not cids:
        return {}

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Optimization: One single query for all requested channels
    # instead of N separate queries in a loop.
    programmes = Programme.query.filter(
        Programme.channel_id.in_(cids),
        Programme.stop > now
    ).order_by(Programme.start).all()

    # Group the results in Python and limit to 4 per channel for the preview
    res = defaultdict(list)
    for p in programmes:
        if len(res[p.channel_id]) < 4:
            res[p.channel_id].append({
                'title': p.title,
                'time': f"{p.start.strftime('%H:%M')} - {p.stop.strftime('%H:%M')}"
            })
    return dict(res)

@app.route('/admin/channel/update_metadata', methods=['POST'])
@login_required
@admin_required
def admin_update_channel_metadata():
    cid = request.form.get('channel_id')
    new_name = request.form.get('name')
    new_logo = request.form.get('logo_url')
    new_tvg_id = request.form.get('tvg_id')
    
    chan = db.session.get(Channel, cid)
    if not chan:
        return abort(404)
        
    chan.logo_override = new_logo if new_logo else None
    
    if chan.group_id:
        group = db.session.get(ChannelGroup, chan.group_id)
        if group:
            if new_name: group.canonical_name = new_name
            if new_tvg_id: group.tvg_id = new_tvg_id
    else:
        chan.name_override = new_name if new_name else None
        chan.tvg_id_override = new_tvg_id if new_tvg_id else None
        
    db.session.commit()
    flash(f"Metadata updated for {chan.name}")
    return redirect(url_for('admin', q=request.form.get('q'), country_id=request.form.get('country_id'), 
                            page=request.form.get('page'), per_page=request.form.get('per_page')) + "#editor")

@app.route('/admin/channel/hide', methods=['POST'])
@login_required
@admin_required
def admin_hide_channels():
    channel_ids = request.form.getlist('channel_ids')
    if channel_ids:
        Channel.query.filter(Channel.id.in_(channel_ids)).update({Channel.is_hidden: True}, synchronize_session=False)
        # Clean up data immediately to save space
        Programme.query.filter(Programme.channel_id.in_(channel_ids)).delete(synchronize_session=False)
        db.session.execute(db.delete(user_channels).where(user_channels.c.channel_id.in_(channel_ids)))
        db.session.commit()
        flash(f"Hidden {len(channel_ids)} channels and purged their data.")

    target_tab = request.form.get('target_tab', 'grouping')
    return redirect(url_for('admin', q=request.form.get('q'), country_id=request.form.get('country_id'), 
                            page=request.form.get('page'), per_page=request.form.get('per_page')) + f"#{target_tab}")

@app.route('/admin/channel/<id>/unhide', methods=['POST'])
@login_required
@admin_required
def admin_unhide_channel(id):
    chan = db.session.get(Channel, id)
    if chan:
        chan.is_hidden = False
        db.session.commit()
        flash(f"Channel {chan.name} re-enabled. It will populate on the next sync.")
    return redirect(url_for('admin'))

@app.route('/admin/source/add', methods=['POST'])
@login_required
@admin_required
def admin_add_source():
    name = request.form.get('name')
    url = request.form.get('url')
    country_id = request.form.get('country_id', type=int)
    if url:
        new_source = Source(name=name, url=url, country_id=country_id, is_syncing=True)
        db.session.add(new_source)
        db.session.commit()
        sync_progress[new_source.id] = {'phase': 'Queued', 'channels': 0, 'progs': 0, 'done': False, 'error': None}
        sync_queue.put(('SINGLE', new_source.id))
        flash('Source added and sync queued.')
    return redirect(url_for('admin'))

@app.route('/admin/source/<int:source_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_source(source_id):
    source = db.session.get(Source, source_id)
    if source and not source.is_syncing:
        source.is_syncing = True
        db.session.commit()
        sync_progress[source_id] = {'phase': 'Queued for Deletion', 'done': False, 'error': None}
        sync_queue.put(('DELETE', source_id))
        flash('Source removal started in background.')
    return redirect(url_for('admin'))

@app.route('/admin/source/<int:source_id>/edit', methods=['POST'])
@login_required
@admin_required
def admin_edit_source(source_id):
    source = db.session.get(Source, source_id)
    new_name = request.form.get('name')
    new_url = request.form.get('url')
    refresh = request.form.get('refresh_interval')
    country_id = request.form.get('country_id', type=int)
    if source:
        source.name = new_name
        source.url = new_url
        source.refresh_interval = int(refresh) if refresh else 24
        if country_id: source.country_id = country_id
        db.session.commit()
        flash('Source updated.')
    return redirect(url_for('admin'))

@app.route('/admin/collection/add', methods=['POST'])
@login_required
@admin_required
def admin_add_collection():
    name = request.form.get('name')
    desc = request.form.get('description')
    if name:
        db.session.add(ChannelCollection(name=name, description=desc))
        db.session.commit()
        flash("Collection created.")
    return redirect(url_for('admin') + "#collections")

@app.route('/admin/collection/<int:coll_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_collection(coll_id):
    coll = db.session.get(ChannelCollection, coll_id)
    if coll: db.session.delete(coll); db.session.commit(); flash("Collection deleted.")
    return redirect(url_for('admin') + "#collections")

@app.route('/admin/collection/<int:coll_id>/sync_channels', methods=['POST'])
@login_required
@admin_required
def admin_sync_collection_channels(coll_id):
    """Updates, adds, or removes channels from a collection."""
    coll = db.session.get(ChannelCollection, coll_id)
    channel_ids = request.form.getlist('channel_ids')
    mode = request.form.get('mode', 'sync') # 'sync' (overwrite), 'add', or 'remove'
    
    if coll:
        channels = Channel.query.filter(Channel.id.in_(channel_ids)).all()
        if mode == 'sync':
            coll.channels = channels
            msg = f"Collection '{coll.name}' overwritten with {len(channels)} channels."
        elif mode == 'add':
            for c in channels:
                if c not in coll.channels: coll.channels.append(c)
            msg = f"Added {len(channels)} channels to '{coll.name}'."
        elif mode == 'remove':
            for c in channels:
                if c in coll.channels: coll.channels.remove(c)
            msg = f"Removed {len(channels)} channels from '{coll.name}'."
        
        db.session.commit()
        flash(msg)
    return redirect(url_for('admin') + "#collections")

@app.route('/admin/category/add', methods=['POST'])
@login_required
@admin_required
def admin_add_category():
    name = request.form.get('name')
    if not name:
        flash("Category name is required.")
        return redirect(url_for('admin') + "#editor")
    
    existing = Category.query.filter_by(name=name).first()
    if existing:
        flash(f"Category '{name}' already exists.")
    else:
        try:
            db.session.add(Category(name=name))
            db.session.commit()
            flash(f"Category '{name}' created.")
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating category: {e}")
            
    return redirect(url_for('admin') + "#editor")

@app.route('/admin/category/<int:cat_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_category(cat_id):
    cat = db.session.get(Category, cat_id)
    if cat: db.session.delete(cat); db.session.commit(); flash("Category deleted.")
    return redirect(url_for('admin') + "#editor")

@app.route('/admin/channel/bulk_tag_category', methods=['POST'])
@login_required
@admin_required
def admin_bulk_tag_category():
    """Adds or removes a category from selected channels."""
    channel_ids = request.form.getlist('channel_ids')
    cat_id = request.form.get('category_id', type=int)
    mode = request.form.get('mode', 'add') # 'add' or 'remove'
    
    if not channel_ids or not cat_id:
        flash("Channels or Category missing.")
        return redirect(url_for('admin') + "#editor")

    cat = db.session.get(Category, cat_id)
    channels = Channel.query.filter(Channel.id.in_(channel_ids)).all()
    
    for chan in channels:
        if mode == 'add':
            if cat not in chan.categories:
                chan.categories.append(cat)
        else:
            if cat in chan.categories:
                chan.categories.remove(cat)
    
    db.session.commit()
    flash(f"Updated category '{cat.name}' for {len(channels)} channels.")
    return redirect(url_for('admin') + "#editor")

@app.route('/admin/collection/<int:coll_id>/channel_ids')
@login_required
@admin_required
def admin_get_collection_ids(coll_id):
    """Returns a list of channel IDs currently in the collection."""
    coll = db.session.get(ChannelCollection, coll_id)
    if not coll: return abort(404)
    return [c.id for c in coll.channels]

@app.route('/admin/category/<int:cat_id>/channel_ids')
@login_required
@admin_required
def admin_get_category_ids(cat_id):
    """Returns a list of channel IDs currently in the category."""
    cat = db.session.get(Category, cat_id)
    if not cat: return abort(404)
    return [c.id for c in cat.channels]

@app.route('/admin/group_queue/add', methods=['POST'])
@login_required
@admin_required
def admin_add_group_job():
    """Adds selected channels to the grouping queue."""
    channel_ids = request.form.getlist('channel_ids')
    if not channel_ids:
        flash("No channels selected for grouping.")
        return redirect(url_for('admin') + "#grouping")

    # Filter out any channels that are already part of a group and are not primary
    # This prevents queuing channels that are already members of a group
    existing_grouped_channels = Channel.query.filter(
        Channel.id.in_(channel_ids),
        Channel.group_id != None,
        Channel.id != ChannelGroup.primary_channel_id # Exclude primary channels of existing groups
    ).all()

    filtered_channel_ids = [cid for cid in channel_ids if not any(c.id == cid for c in existing_grouped_channels)]

    if not filtered_channel_ids:
        flash("Selected channels are already grouped or invalid for queuing.")
        return redirect(url_for('admin') + "#grouping")

    new_job = GroupingJob(channel_ids=filtered_channel_ids)
    db.session.add(new_job)
    db.session.commit()
    flash(f"Grouping job for {len(filtered_channel_ids)} channels queued.")

    return redirect(url_for('admin', q=request.form.get('q'), country_id=request.form.get('country_id'), 
                            page=request.form.get('page'), per_page=request.form.get('per_page')) + "#grouping")

@app.route('/admin/group_queue/details/<int:job_id>')
@login_required
@admin_required
def admin_group_job_details(job_id):
    """Returns details for a specific grouping job."""
    job = db.session.get(GroupingJob, job_id)
    if not job:
        return abort(404)

    channels_data = []
    for cid in job.channel_ids:
        channel = db.session.get(Channel, cid)
        if channel:
            channels_data.append({
                "id": channel.id,
                "name": channel.name,
                "sources": ", ".join(channel.all_source_names),
                "is_group": False, # When queuing, they are individual channels
                "primary_id": None,
                "tvg_id": channel.tvg_id_override if channel.tvg_id_override else channel.id,
                "logo_override": channel.logo_override
            })
    return channels_data

@app.route('/admin/group_queue/delete/<int:job_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_group_job(job_id):
    job = db.session.get(GroupingJob, job_id)
    if job: db.session.delete(job); db.session.commit(); flash("Grouping job discarded.")
    return redirect(url_for('admin') + "#grouping")

@app.route('/admin/sync')
@login_required
@admin_required
def sync_all():
    sync_queue.put(('ALL', None))
    flash('Full background sync started.')
    return redirect(url_for('admin'))

@app.route('/admin/source/<int:source_id>/refresh', methods=['POST'])
@login_required
@admin_required
def admin_refresh_source(source_id):
    source = db.session.get(Source, source_id)
    if source:
        if source.is_syncing:
            flash('Source is already syncing.')
        else:
            source.is_syncing = True
            db.session.commit()
            sync_progress[source_id] = {'phase': 'Queued', 'channels': 0, 'progs': 0, 'done': False, 'error': None}
            sync_queue.put(('SINGLE', source_id))
            flash('Refresh queued.')
    return redirect(url_for('admin'))

@app.route('/admin/source/<int:source_id>/progress')
@login_required
@admin_required
def source_progress(source_id):
    """Returns live import progress as JSON. Polled by the admin UI."""
    import json
    try:
        db.session.expire_all() # Fetch freshest stats from DB
        source = db.session.get(Source, source_id)
        progress = sync_progress.get(source_id, None)

        if not source:
            # If the source is gone from DB, but we have a final "Deleted" status in memory, return that
            if progress and (progress.get('phase') == 'Deleted' or progress.get('done')):
                return Response(json.dumps({
                    'is_syncing': False, 'done': True, 'phase': 'Deleted', 'channel_count': 0, 'prog_count': 0
                }), mimetype='application/json')
            return Response(json.dumps({'error': 'not found'}), mimetype='application/json', status=404)

        payload = {
            'is_syncing': source.is_syncing,
            'last_error': source.last_error,
            'channel_count': progress.get('channel_count') if progress and 'channel_count' in progress else source.channel_count,
            'prog_count': progress.get('prog_count') if progress and 'prog_count' in progress else source.prog_count,
        }
        if progress:
            payload.update({
                'phase': progress.get('phase'),
                'live_channels': progress.get('channels', 0),
                'live_progs': progress.get('progs', 0),
                'done': progress.get('done', False) or progress.get('phase') == 'Complete',
                'error': progress.get('error'),
            })
    except Exception as e:
        # If DB is busy or source is missing, don't trigger a UI deletion
        return Response(json.dumps({'error': 'database unavailable', 'is_syncing': True}), mimetype='application/json')

    return Response(json.dumps(payload), mimetype='application/json')

# --- Background Scheduler ---

sync_queue = queue.Queue()

# Tracks live progress per source_id: { source_id: { 'phase': str, 'channels': int, 'progs': int, 'done': bool, 'error': str|None } }
sync_progress = {}

def sync_worker():
    """Serializes all sync operations to avoid SQLite locking and UI blocking."""
    while True:
        task = sync_queue.get()
        if task is None: break
        
        task_type, payload = task
        with app.app_context():
            try:
                if task_type == 'SINGLE':
                    # Refresh from DB to get the latest state
                    db.session.expire_all()
                    source = db.session.get(Source, payload)
                    if source:
                        try:
                            source.is_syncing = True
                            db.session.commit()
                            sync_source(source, set())
                        finally:
                            source.is_syncing = False
                            db.session.commit()
                elif task_type == 'ALL':
                    db.session.expire_all()
                    sources = Source.query.all()
                    if sources:
                        # Only clear programmes if we actually have sources to sync
                        db.session.query(Programme).delete()
                        db.session.commit()
                        updated_ids = set()
                        for s in sources:
                            try:
                                s.is_syncing = True
                                db.session.commit()
                                sync_source(s, updated_ids)
                            finally:
                                s.is_syncing = False
                                db.session.commit()
                elif task_type == 'DELETE':
                    perform_source_deletion(payload)
            except Exception as e:
                print(f"Worker error: {e}")
                db.session.rollback()
            finally:
                db.session.remove() # Clean up session
                sync_queue.task_done()

def run_scheduler():
    """Background thread to auto-refresh sources."""
    last_prune_time = 0
    while True:
        with app.app_context():
            now = datetime.now(timezone.utc)
            due_sources = Source.query.filter(Source.next_update <= now, Source.is_syncing == False).all()
            if due_sources:
                for s in due_sources:
                    sync_queue.put(('SINGLE', s.id))
            
            # Prune programs based on configurable retention days once per hour
            if time.time() - last_prune_time > 3600:
                days = int(get_setting('epg_retention_days', '2'))
                cutoff = now.replace(tzinfo=None) - timedelta(days=days)
                Programme.query.filter(Programme.stop < cutoff).delete()
                db.session.commit()
                last_prune_time = time.time()

        time.sleep(60) # Check every minute

@app.route('/admin/user/<int:user_id>/action', methods=['POST'])
@login_required
@admin_required
def admin_user_action(user_id):
    user = db.session.get(User, user_id)
    if not user: return redirect(url_for('admin'))
    
    action = request.form.get('action')
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if action == 'toggle_status': user.is_active = not user.is_active
    elif action == 'toggle_admin': user.is_admin = not user.is_admin
    elif action == 'upgrade_1m':
        user.tier = 'premium'
        base = user.premium_expiry if user.premium_expiry and user.premium_expiry > now else now
        user.premium_expiry = base + timedelta(days=30)
    elif action == 'upgrade_1y':
        user.tier = 'premium'
        base = user.premium_expiry if user.premium_expiry and user.premium_expiry > now else now
        user.premium_expiry = base + timedelta(days=365)
    elif action == 'downgrade': user.tier = 'free'; user.premium_expiry = None
    elif action == 'set_contributor': user.tier = 'contributor'; user.premium_expiry = None
    elif action == 'set_sponsor': user.tier = 'sponsor'; user.premium_expiry = None
    elif action == 'delete' and user.username != 'admin': 
        db.session.delete(user)
    elif action == 'reset_password':
        new_pass = request.form.get('new_password')
        if new_pass: user.password_hash = generate_password_hash(new_pass)
    
    db.session.commit()
    flash('User updated.')
    return redirect(url_for('admin'))

@app.route('/admin/settings', methods=['POST'])
@login_required
@admin_required
def admin_settings():
    limits = {
        'registration_enabled': request.form.get('registration_enabled', 'false'),
        'free_tier_limit': request.form.get('free_tier_limit', '200'),
        'premium_tier_limit': request.form.get('premium_tier_limit', '500'),
        'contributor_tier_limit': request.form.get('contributor_tier_limit', '1000'),
        'sponsor_tier_limit': request.form.get('sponsor_tier_limit', '99999'),
        'epg_retention_days': request.form.get('epg_retention_days', '2'),
        'domain_name': request.form.get('domain_name', ''),
        'github_token': request.form.get('github_token', '')
    }
    for k, v in limits.items():
        setting = Setting.query.filter_by(key=k).first()
        if setting: setting.value = v
        else: db.session.add(Setting(key=k, value=v))
        
    db.session.commit()
    flash('Settings updated.')
    return redirect(url_for('admin'))

@app.route('/admin/group_channels', methods=['POST'])
@login_required
@admin_required
def group_channels():
    channel_ids = request.form.getlist('channel_ids') 
    canonical_name = request.form.get('canonical_name')
    primary_channel_id = request.form.get('primary_channel_id')
    logo_url = request.form.get('logo_url')
    tvg_id = request.form.get('tvg_id')
    job_id = request.form.get('job_id')
    
    if not channel_ids or not primary_channel_id:
        flash("Error: No channels or primary source selected.")
        return redirect(url_for('admin'))

    # 1. Identify if we are updating an existing group
    group = None
    # Check if any of the provided channels are already in a group
    existing_group_link = Channel.query.filter(Channel.id.in_(channel_ids), Channel.group_id != None).first()
    if existing_group_link:
        group = existing_group_link.group

    primary_changed = False
    if not group:
        group = ChannelGroup(canonical_name=canonical_name, tvg_id=tvg_id, primary_channel_id=primary_channel_id, logo_override=logo_url)
        db.session.add(group)
        db.session.flush()
    else:
        if group.primary_channel_id != primary_channel_id:
            primary_changed = True
        group.canonical_name = canonical_name
        group.tvg_id = tvg_id
        group.logo_override = logo_url if logo_url else None
        group.primary_channel_id = primary_channel_id

    # 2. Assign all selected channels to this group
    Channel.query.filter(Channel.id.in_(channel_ids)).update({Channel.group_id: group.id}, synchronize_session=False)
    db.session.commit()

    # 3. Update user selections: Migrate users from non-primaries to the primary
    non_primaries = [cid for cid in channel_ids if cid != primary_channel_id]
    if non_primaries:
        # Migrate selections in user_channels table
        for np_id in non_primaries:
            # For users with the non-primary selected, ensure they have the primary instead
            db.session.execute(db.text("""
                INSERT INTO user_channels (user_id, channel_id)
                SELECT user_id, :primary FROM user_channels WHERE channel_id = :np
                ON CONFLICT DO NOTHING
            """), {"primary": primary_channel_id, "np": np_id})
            
            # Remove the non-primary selection
            db.session.execute(db.delete(user_channels).where(user_channels.c.channel_id == np_id))

    # 4. If this grouping came from a queued job, remove the job
    if job_id:
        job = db.session.get(GroupingJob, int(job_id))
        if job:
            db.session.delete(job)

    if primary_changed:
        flash(f'Group "{canonical_name}" updated. Master channel changed.')
    else:
        flash(f'Group "{canonical_name}" saved.')

    db.session.commit()

    return redirect(url_for('admin', q=request.form.get('q'), country_id=request.form.get('country_id'), 
                            page=request.form.get('page'), per_page=request.form.get('per_page')) + "#grouping")

@app.route('/admin/ungroup/<int:group_id>', methods=['POST'])
@login_required
@admin_required
def ungroup_channels(group_id):
    """Dissolves a channel group, returning all member channels to standalone status."""
    group = db.session.get(ChannelGroup, group_id)
    if not group:
        flash('Group not found.')
        return redirect(url_for('admin') + '#grouping')

    group_name = group.canonical_name

    # Detach all member channels from the group
    Channel.query.filter(Channel.group_id == group_id).update(
        {Channel.group_id: None}, synchronize_session=False
    )

    # Delete the group record
    db.session.delete(group)
    db.session.commit()

    flash(f'Group "{group_name}" has been dissolved. All channels restored to individual status.')
    return redirect(url_for('admin', q=request.form.get('q'), country_id=request.form.get('country_id'),
                            page=request.form.get('page'), per_page=request.form.get('per_page')) + '#grouping')


@app.route('/admin/backup/export')
@login_required
@admin_required
def admin_backup_export():
    """Exports configuration tables to a JSON file."""
    data = {
        'settings': [dict(row.__dict__) for row in Setting.query.all()],
        'users': [dict(row.__dict__) for row in User.query.all()],
        'countries': [dict(row.__dict__) for row in Country.query.all()],
        'sources': [dict(row.__dict__) for row in Source.query.all()],
        'channel_groups': [dict(row.__dict__) for row in ChannelGroup.query.all()],
        'channels': [dict(row.__dict__) for row in Channel.query.all()],
        'mappings': [dict(row.__dict__) for row in SourceChannelMapping.query.all()],
        'user_channels': [dict(r._mapping) for r in db.session.execute(db.select(user_channels.c.user_id, user_channels.c.channel_id)).all()]
    }

    for table in data:
        if table == 'user_channels': continue
        for item in data[table]:
            item.pop('_sa_instance_state', None)
            for k, v in item.items():
                if isinstance(v, datetime):
                    item[k] = v.isoformat()

    json_str = json.dumps(data, indent=2)
    return Response(
        json_str,
        mimetype="application/json",
        headers={"Content-disposition": f"attachment; filename=epg_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"}
    )

@app.route('/admin/backup/import', methods=['POST'])
@login_required
@admin_required
def admin_backup_import():
    """Restores configuration from a JSON file, wiping existing data."""
    file = request.files.get('backup_file')
    if not file:
        flash("No file provided.")
        return redirect(url_for('admin') + "#backup")
    
    try:
        data = json.load(file)
        
        # Clear existing data in reverse dependency order
        db.session.execute(db.delete(user_channels))
        Programme.query.delete()
        SourceChannelMapping.query.delete()
        Source.query.delete()
        Channel.query.delete()
        ChannelGroup.query.delete()
        User.query.delete()
        Country.query.delete()
        Setting.query.delete()
        db.session.commit()

        def parse_date(v):
            if isinstance(v, str) and 'T' in v:
                try: return datetime.fromisoformat(v)
                except: return v
            return v

        # Restore tables in dependency order
        for item in data.get('settings', []): db.session.add(Setting(**item))
        for item in data.get('countries', []): db.session.add(Country(**item))
        for item in data.get('users', []):
            item['premium_expiry'] = parse_date(item.get('premium_expiry'))
            db.session.add(User(**item))
        for item in data.get('channel_groups', []): db.session.add(ChannelGroup(**item))
        for item in data.get('channels', []): db.session.add(Channel(**item))
        for item in data.get('sources', []):
            item['last_updated'] = parse_date(item.get('last_updated'))
            item['next_update'] = parse_date(item.get('next_update'))
            item['is_syncing'] = False # Reset syncing state on restore
            db.session.add(Source(**item))
        db.session.commit()

        for item in data.get('mappings', []): db.session.add(SourceChannelMapping(**item))
        for item in data.get('user_channels', []): db.session.execute(db.insert(user_channels).values(**item))
        db.session.commit()

        flash("Database configuration restored successfully. Please trigger a sync to restore EPG data.")
    except Exception as e:
        db.session.rollback()
        flash(f"Restore failed: {str(e)}")
    return redirect(url_for('admin') + "#backup")

@app.route('/export.xml')
def export_xml():
    return Response(generate_xmltv_content(Channel.query.all()), mimetype='text/xml')

@app.route('/favicon.ico')
def favicon():
    """Satisfies browser requests for the site icon to prevent 404s."""
    return make_response("", 204)

@app.route('/xml/<username>/<api_key>.xml')
def download_user_xml(username, api_key):
    user = User.query.filter_by(username=username, api_key=api_key, is_active=True).first_or_404()
    return Response(generate_xmltv_content(user.selected_channels), mimetype='text/xml')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Populate Countries table if empty
        if not Country.query.first():
            for code, label in COUNTRIES.items():
                parts = label.split(' ', 1)
                flag = parts[0] if len(parts) > 1 else ''
                name = parts[1] if len(parts) > 1 else label
                db.session.add(Country(code=code, name=name, flag=flag))
            db.session.commit()

        if not User.query.filter_by(username='admin').first():
            db.session.add(User(username='admin', password_hash=generate_password_hash('admin123'), is_admin=True, api_key=secrets.token_hex(16)))
        if not Setting.query.filter_by(key='registration_enabled').first():
            db.session.add(Setting(key='registration_enabled', value='true'))
        if not Setting.query.filter_by(key='free_tier_limit').first():
            db.session.add(Setting(key='free_tier_limit', value='200'))
        if not Setting.query.filter_by(key='premium_tier_limit').first():
            db.session.add(Setting(key='premium_tier_limit', value='500'))
        if not Setting.query.filter_by(key='contributor_tier_limit').first():
            db.session.add(Setting(key='contributor_tier_limit', value='1000'))
        if not Setting.query.filter_by(key='sponsor_tier_limit').first():
            db.session.add(Setting(key='sponsor_tier_limit', value='99999'))
        if not Setting.query.filter_by(key='domain_name').first():
            db.session.add(Setting(key='domain_name', value=''))
        if not Setting.query.filter_by(key='github_token').first():
            db.session.add(Setting(key='github_token', value=''))
        if not Setting.query.filter_by(key='epg_retention_days').first():
            db.session.add(Setting(key='epg_retention_days', value='2'))
        db.session.commit()

    # --- Security Initialization (Must be before app.run) ---
    _whitelist_env = os.environ.get('WHITELIST_IPS', '127.0.0.1')
    WHITELIST_IPS = [ip.strip() for ip in _whitelist_env.split(',') if ip.strip()]
    sec.init_security(app, db, BannedIP, WHITELIST_IPS, get_current_user)

    threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    app.run(host='0.0.0.0', port=5000, debug=False) # Recommend debug off for WAN