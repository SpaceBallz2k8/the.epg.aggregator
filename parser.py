from lxml import etree
from models import db, Channel, Programme
import io
from datetime import datetime, timezone
import re

def parse_xmltv_date(date_str):
    """Parses XMLTV date strings into datetime objects using UTC."""
    if not date_str:
        return None
    try:
        # Standardize: handle 'Z', and colons in offsets (+01:00 -> +0100)
        clean_date = date_str.strip()
        if clean_date.endswith('Z'):
            clean_date = clean_date[:-1] + ' +0000'
        
        clean_date = re.sub(r'([+=-]\d{2}):(\d{2})$', r' \1\2', clean_date)
        clean_date = clean_date.replace('=', '+')
        
        if ' ' not in clean_date and any(c in clean_date for c in '+-'):
            clean_date = re.sub(r'([+-])', r' \1', clean_date)
        
        for fmt in ["%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z", "%Y%m%d %z"]:
            try:
                dt = datetime.strptime(clean_date, fmt)
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                continue

        return datetime.strptime(clean_date[:14], "%Y%m%d%H%M%S")
    except Exception:
        return None

def get_text_list(element, tag):
    return [node.text for node in element.findall(tag) if node.text]

def parse_xmltv_stream(stream):
    """Memory-efficient parser using iterparse."""
    context = etree.iterparse(stream, events=('end',), tag=('channel', 'programme'))
    
    prog_buffer = []
    for event, elem in context:
        if elem.tag == 'channel':
            process_channel(elem)
        elif elem.tag == 'programme':
            prog = process_programme(elem)
            if prog:
                prog_buffer.append(prog)
        
        if len(prog_buffer) >= 500:
            db.session.bulk_save_objects(prog_buffer)
            db.session.commit()
            prog_buffer = []
        
        # Clear memory
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
            
    if prog_buffer:
        db.session.bulk_save_objects(prog_buffer)
    db.session.commit()

def process_channel(elem):
    xml_id = elem.get('id')
    if not xml_id:
        return

    data = {
        'xmltv_id': xml_id,
        'display_names': get_text_list(elem, 'display-name'),
        'icon_url': elem.find('icon').get('src') if elem.find('icon') is not None else None,
        'urls': get_text_list(elem, 'url'),
        # Heuristic for country if not present: usually derived from provider or URL
        'country': elem.findtext('country') or "Unknown" 
    }

    chan = Channel.query.filter_by(xmltv_id=xml_id).first()
    if not chan:
        db.session.add(Channel(**data))
    else:
        for key, val in data.items():
            setattr(chan, key, val)

def process_programme(elem):
    # Extract Credits
    credits_dict = {}
    credits_node = elem.find('credits')
    if credits_node is not None:
        for person in credits_node:
            role = person.tag
            credits_dict.setdefault(role, []).append(person.text)

    # Extract Episode Numbers
    ep_nums = []
    for ep in elem.findall('episode-num'):
        ep_nums.append({'system': ep.get('system'), 'value': ep.text})

    # Extract Ratings
    ratings = []
    for r in elem.findall('rating'):
        ratings.append({
            'system': r.get('system'), 
            'value': r.findtext('value'),
            'icons': [i.get('src') for i in r.findall('icon')]
        })

    start_str = elem.get('start')
    stop_str = elem.get('stop')
    start_dt = parse_xmltv_date(start_str)
    stop_dt = parse_xmltv_date(stop_str)
    channel_id = elem.get('channel')

    return Programme(
        channel_xmltv_id=channel_id,
        start=start_dt,
        stop=stop_dt,
        title=elem.findtext('title'),
        sub_title=elem.findtext('sub-title'),
        description=elem.findtext('desc'),
        date=elem.findtext('date'),
        categories=get_text_list(elem, 'category'),
        credits=credits_dict,
        episode_nums=ep_nums,
        ratings=ratings,
        star_rating=elem.findtext('star-rating/value'),
        is_new=elem.find('new') is not None,
        previously_shown=elem.find('previously-shown') is not None,
        premiere=elem.find('premiere') is not None,
        icons=[i.get('src') for i in elem.findall('icon') if i.get('src')]
    )