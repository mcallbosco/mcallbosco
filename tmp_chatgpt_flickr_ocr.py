from __future__ import annotations

import json
import os
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import img2pdf
import requests
from PIL import Image, ImageOps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ALBUM_URL = os.environ['ALBUM_URL']
PHOTOSET_ID = os.environ['PHOTOSET_ID']
USER_ID = os.environ['FLICKR_USER_ID']
OUT_PDF = Path('/tmp/album_image_only.pdf')
RAW_DIR = Path('/tmp/flickr_raw')
NORM_DIR = Path('/tmp/flickr_normalized')
RAW_DIR.mkdir(parents=True, exist_ok=True)
NORM_DIR.mkdir(parents=True, exist_ok=True)

session = requests.Session()
retry = Retry(
    total=8,
    connect=8,
    read=8,
    status=8,
    backoff_factor=1.2,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({'GET'}),
    respect_retry_after_header=True,
)
session.mount('https://', HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36',
    'Referer': ALBUM_URL,
    'Accept': '*/*',
})

def discover_api_key() -> str:
    candidates = ['9d5522296a7b6e5af504263952122e1c']
    for page_url in ('https://www.flickr.com/', 'https://www.flickr.com/photos/'):
        try:
            text = session.get(page_url, timeout=45).text
        except requests.RequestException:
            continue
        patterns = (
            r'root\.YUI_config\.flickr\.api\.site_key\s*=\s*["\']([^"\']+)',
            r'["\']api_key["\']\s*:\s*["\']([0-9a-f]{32})',
            r'["\']site_key["\']\s*:\s*["\']([0-9a-f]{32})',
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match and match.group(1) not in candidates:
                candidates.insert(0, match.group(1))
    return candidates[0]

def get_manifest() -> tuple[dict[str, Any], str]:
    api_keys = []
    discovered = discover_api_key()
    for key in (discovered, '9d5522296a7b6e5af504263952122e1c', 'c149b994c54c114bd7836b61539eec2e'):
        if key and key not in api_keys:
            api_keys.append(key)

    last_error = ''
    for api_key in api_keys:
        params = {
            'method': 'flickr.photosets.getPhotos',
            'api_key': api_key,
            'photoset_id': PHOTOSET_ID,
            'user_id': USER_ID,
            'extras': ','.join((
                'description', 'date_upload', 'date_taken', 'original_format', 'o_dims',
                'url_o', 'url_6k', 'url_5k', 'url_4k', 'url_3k',
                'url_k', 'url_h', 'url_l', 'url_c', 'url_z',
            )),
            'per_page': 500,
            'page': 1,
            'format': 'json',
            'nojsoncallback': 1,
            'hermes': 1,
            'hermesClient': 1,
        }
        response = session.get('https://api.flickr.com/services/rest/', params=params, timeout=90)
        response.raise_for_status()
        data = response.json()
        if data.get('stat') == 'ok' and data.get('photoset', {}).get('photo'):
            return data, api_key
        last_error = json.dumps(data)[:1000]
    raise RuntimeError(f'Flickr API failed for all public keys: {last_error}')

manifest, used_key = get_manifest()
photoset = manifest['photoset']
photos = photoset['photo']
expected_total = int(photoset.get('total', len(photos)))
if expected_total != len(photos):
    raise RuntimeError(f'Expected {expected_total} photos, API returned {len(photos)}')
if len(photos) != 184:
    raise RuntimeError(f'Album changed or incomplete: expected 184 photos, got {len(photos)}')

preferred_keys = ('url_k', 'url_h', 'url_l', 'url_c', 'url_z', 'url_o')
normalized_paths: list[str] = []
metadata: list[dict[str, Any]] = []

for index, photo in enumerate(photos, start=1):
    chosen_key = next((key for key in preferred_keys if photo.get(key)), None)
    if not chosen_key:
        raise RuntimeError(f'No downloadable image URL for photo {photo.get("id")}')
    url = photo[chosen_key]
    suffix = Path(url.split('?', 1)[0]).suffix.lower() or '.jpg'
    raw_path = RAW_DIR / f'{index:04d}_{photo["id"]}{suffix}'
    norm_path = NORM_DIR / f'{index:04d}_{photo["id"]}.jpg'

    response = session.get(url, timeout=120)
    response.raise_for_status()
    raw_path.write_bytes(response.content)

    with Image.open(BytesIO(response.content)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        elif image.mode == 'L':
            image = image.convert('RGB')
        max_dimension = 2400
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        image.save(norm_path, 'JPEG', quality=90, optimize=True, progressive=False, subsampling=0, dpi=(200, 200))

    normalized_paths.append(str(norm_path))
    with Image.open(norm_path) as normalized_image:
        normalized_width, normalized_height = normalized_image.size

    metadata.append({
        'page': index,
        'photo_id': photo.get('id'),
        'title': photo.get('title', ''),
        'source_url': f'https://www.flickr.com/photos/dolinick/{photo.get("id")}/in/album-{PHOTOSET_ID}',
        'download_url': url,
        'download_size': chosen_key,
        'normalized_width': normalized_width,
        'normalized_height': normalized_height,
    })
    if index % 10 == 0 or index == len(photos):
        print(f'Downloaded and normalized {index}/{len(photos)}', flush=True)
    time.sleep(0.15)

layout = img2pdf.get_fixed_dpi_layout_fun((200, 200))
OUT_PDF.write_bytes(img2pdf.convert(normalized_paths, layout_fun=layout))

out_metadata = {
    'album_url': ALBUM_URL,
    'photoset_id': PHOTOSET_ID,
    'photo_count': len(photos),
    'api_key_used': used_key,
    'photos': metadata,
}
Path('/tmp/album_metadata.json').write_text(json.dumps(out_metadata, indent=2), encoding='utf-8')
print(f'Created image-only PDF with {len(photos)} pages: {OUT_PDF}', flush=True)
