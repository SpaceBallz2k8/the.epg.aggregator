from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()

class Channel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    xmltv_id = db.Column(db.String(100), unique=True, nullable=False)
    display_names = db.Column(db.JSON)  # List of names
    icon_url = db.Column(db.String(500))
    urls = db.Column(db.JSON)           # List of website URLs
    country = db.Column(db.String(100), index=True)

class Programme(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_xmltv_id = db.Column(db.String(100), db.ForeignKey('channel.xmltv_id'), index=True)
    
    # Timing
    start = db.Column(db.DateTime, index=True)
    stop = db.Column(db.DateTime)
    
    # Content
    title = db.Column(db.String(500))
    sub_title = db.Column(db.String(500))
    description = db.Column(db.Text)
    date = db.Column(db.String(50))
    
    # Detailed Metadata (Stored as JSON)
    categories = db.Column(db.JSON)     # ['Movie', 'Action']
    credits = db.Column(db.JSON)        # {'actors': [], 'directors': []}
    episode_nums = db.Column(db.JSON)   # [{'system': 'xmltv_ns', 'value': '...'}]
    icons = db.Column(db.JSON)          # List of image URLs
    ratings = db.Column(db.JSON)        # [{'system': 'vchip', 'value': 'TV-MA'}]
    star_rating = db.Column(db.String(50))
    
    # Qualifiers
    is_new = db.Column(db.Boolean, default=False)
    previously_shown = db.Column(db.Boolean, default=False)
    premiere = db.Column(db.Boolean, default=False)

    # Add indices for faster lookups during export
    __table_args__ = (
        db.Index('idx_prog_lookup', 'channel_xmltv_id', 'start'),
        UniqueConstraint('channel_xmltv_id', 'start', name='_channel_start_uc'),
    )