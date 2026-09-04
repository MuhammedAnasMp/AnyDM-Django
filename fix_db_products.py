import os
import sys
import django
import requests

# Set up Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apps.products.models import Product
from apps.accounts.models import InstagramAccount
from apps.products.utils import extract_instagram_id

def get_url_signature(url):
    if not url:
        return None
    path = url.split('?')[0]
    filename = path.split('/')[-1]
    if len(filename) > 10:
        return filename
    return None

def fetch_all_instagram_media(access_token, user_id):
    is_basic = access_token.startswith("IGAA")
    host = "graph.instagram.com" if is_basic else "graph.facebook.com"
    url = f"https://{host}/v26.0/{user_id}/media"
    
    fields = "id,caption,media_type,media_url,permalink,timestamp,thumbnail_url,children{id,media_type,media_url,permalink,thumbnail_url}"
    params = {
        "fields": fields,
        "access_token": access_token
    }
    
    media_list = []
    try:
        r = requests.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        media_list.extend(data.get('data', []))
        
        # Paginate a bit if needed (up to 3 pages)
        next_url = data.get('paging', {}).get('next')
        pages = 1
        while next_url and pages < 3:
            r = requests.get(next_url)
            r.raise_for_status()
            data = r.json()
            media_list.extend(data.get('data', []))
            next_url = data.get('paging', {}).get('next')
            pages += 1
    except Exception as e:
        print(f"Error fetching media for token {access_token[:10]}...: {e}")
    
    return media_list

def run():
    print("=== STARTING PRODUCT DB SOURCE_ID FIXER ===")
    
    # 1. Load active instagram accounts
    accounts = InstagramAccount.objects.filter(is_active=True)
    print(f"Found {accounts.count()} active Instagram accounts.")
    
    for acc in accounts:
        user_id = acc.instagram_user_id or acc.instagram_scoped_id
        if not user_id or not acc.access_token:
            print(f"Skipping account @{acc.username} due to missing user ID or token.")
            continue
            
        print(f"Fetching media for @{acc.username}...")
        media_items = fetch_all_instagram_media(acc.access_token, user_id)
        if not media_items:
            print(f"No media found or failed to fetch for @{acc.username}.")
            continue
            
        print(f"Fetched {len(media_items)} media items. Processing products...")
        
        # 2. Get products for this seller (user)
        products = Product.objects.filter(seller=acc.user)
        for p in products:
            # Extract signatures of the product's main media and gallery items
            p_signatures = set()
            sig = get_url_signature(p.main_media_url)
            if sig:
                p_signatures.add(sig)
            for m in p.gallery.all():
                sig = get_url_signature(m.media_url)
                if sig:
                    p_signatures.add(sig)
                sig = get_url_signature(m.thumbnail_url)
                if sig:
                    p_signatures.add(sig)
            
            # If no signatures, skip
            if not p_signatures:
                continue
                
            matched_media = None
            for item in media_items:
                item_urls = []
                if item.get('media_url'):
                    item_urls.append(item['media_url'])
                if item.get('thumbnail_url'):
                    item_urls.append(item['thumbnail_url'])
                
                # Check children
                children = item.get('children', {}).get('data', [])
                for child in children:
                    if child.get('media_url'):
                        item_urls.append(child['media_url'])
                    if child.get('thumbnail_url'):
                        item_urls.append(child['thumbnail_url'])
                
                # Extract signatures from item URLs
                item_signatures = set()
                for url in item_urls:
                    sig = get_url_signature(url)
                    if sig:
                        item_signatures.add(sig)
                        
                # If there's an intersection, we have a match!
                if p_signatures.intersection(item_signatures):
                    matched_media = item
                    break
            
            if matched_media:
                shortcode = extract_instagram_id(matched_media.get('permalink', ''))
                print(f"Matched product '{p.title}' (ID: {p.id}) with Instagram Post '{matched_media.get('id')}'")
                print(f"  Shortcode: {shortcode}")
                print(f"  Permalink: {matched_media.get('permalink')}")
                
                p.source_type = 'REEL'
                p.source_id = shortcode or matched_media.get('id')
                p.instagram_permalink = matched_media.get('permalink')
                p.save()
                print("  Updated successfully.")
            else:
                if p.source_type == 'REEL' and not p.source_id:
                    print(f"Could not match REEL product '{p.title}' (ID: {p.id}) to any fetched Instagram media.")

if __name__ == '__main__':
    run()
