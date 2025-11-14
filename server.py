from flask import Flask, request, jsonify
import requests
import time
import hashlib
import json

app = Flask(__name__)

# ------------------------------
# CONFIG
# ------------------------------
LASTFM_API_KEY = "fc5c523e4ca67f9b8248653c533ad495"
DISCOGS_TOKEN = "NVAgJHtqrslSJuulGGaV"
USER_AGENT = {"User-Agent": "FOURplay-Microservice/1.0"}

# ------------------------------
# CACHE (memory)
# ------------------------------
CACHE = {}
CACHE_TTL = 3600  # 1 hour


def cache_get(key):
    entry = CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > CACHE_TTL:
        return None
    return data


def cache_put(key, value):
    CACHE[key] = (time.time(), value)


# ------------------------------
# HTTP WRAPPER WITH RETRIES
# ------------------------------
def http_get_json(url, headers=None):
    headers = headers or {}
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        time.sleep(0.5)
    return None


# =====================================================================
#                  1. MUSICBRAINZ
# =====================================================================

def mb_lookup_recording(artist, title):
    key = f"mb_rec::{artist.lower()}::{title.lower()}"
    cached = cache_get(key)
    if cached:
        return cached

    query = f'https://musicbrainz.org/ws/2/recording/?query=recording:"{title}" AND artist:"{artist}"&fmt=json&limit=1'
    json_data = http_get_json(query, USER_AGENT)

    if not json_data or "recordings" not in json_data:
        return None

    if len(json_data["recordings"]) == 0:
        return None

    rec = json_data["recordings"][0]

    out = {
        "recordingMBID": rec.get("id"),
        "artistMBID": rec["artist-credit"][0]["artist"]["id"]
            if rec.get("artist-credit") else None,
        "year": extract_year(rec.get("first-release-date", "")),
        "relationScore": 0.5 if rec.get("relations") else 0,
        "eraScore": 0,  # computed later
    }

    cache_put(key, out)
    return out


def extract_year(text):
    import re
    m = re.search(r"(19|20)\d{2}", str(text))
    return int(m.group(0)) if m else 0


def mb_era_score(track_year, content_year):
    if not track_year or not content_year:
        return 0
    diff = abs(track_year - content_year)
    return max(0, 1 - diff / 10.0)


# =====================================================================
#                2. LISTENBRAINZ
# =====================================================================

def lb_fetch_similar(artist):
    key = f"lb_sim::{artist.lower()}"
    cached = cache_get(key)
    if cached:
        return cached

    q = f"https://api.listenbrainz.org/1/search/artist?q={artist}&limit=1"
    data = http_get_json(q)
    if not data or "artists" not in data or not data["artists"]:
        return []

    mbid = data["artists"][0].get("mbid")
    if not mbid:
        return []

    url = f"https://api.listenbrainz.org/1/artist/{mbid}/similar-artists"
    json_data = http_get_json(url)

    if not json_data or "similar_artists" not in json_data:
        return []

    out = [
        {"artist": a.get("artist_name"), "score": float(a.get("score", 0))}
        for a in json_data["similar_artists"][:10]
    ]

    cache_put(key, out)
    return out


def lb_listener_weight(artist):
    # Very rough: LB doesn't expose total listeners per artist easily.
    # We'll mock an approximate weight based on similar-artist count.
    sim = lb_fetch_similar(artist)
    if not sim:
        return 0
    count = len(sim)
    return min(1, count / 10.0)


# =====================================================================
#                   3. DISCOGS
# =====================================================================

def discogs_artist_search(artist):
    url = f"https://api.discogs.com/database/search?type=artist&q={artist}&token={DISCOGS_TOKEN}"
    data = http_get_json(url, USER_AGENT)
    if not data or "results" not in data or not data["results"]:
        return None
    return data["results"][0].get("id")


def discogs_artist_releases(artist):
    key = f"disc_releases::{artist.lower()}"
    cached = cache_get(key)
    if cached:
        return cached

    aid = discogs_artist_search(artist)
    if not aid:
        return []

    url = f"https://api.discogs.com/artists/{aid}/releases?sort=year&per_page=50"
    data = http_get_json(url, USER_AGENT)
    if not data or "releases" not in data:
        return []

    out = []
    for r in data["releases"]:
        if r.get("type") in ["master", "release"]:
            out.append({
                "title": r.get("title"),
                "id": r.get("id"),
                "year": r.get("year")
            })

    cache_put(key, out)
    return out


def discogs_style_score(artist):
    rel = discogs_artist_releases(artist)
    if not rel:
        return 0
    # heuristic: more releases = deeper catalog = scene continuity
    count = len(rel)
    return min(1, count / 50.0)


# =====================================================================
#                      4. LAST.FM
# =====================================================================

def lfm_similar_tracks(artist, title):
    key = f"lfm_sim::{artist.lower()}::{title.lower()}"
    cached = cache_get(key)
    if cached:
        return cached

    url = (
        "https://ws.audioscrobbler.com/2.0/?method=track.getSimilar"
        f"&artist={artist}&track={title}&limit=10&api_key={LASTFM_API_KEY}&format=json"
    )

    data = http_get_json(url)
    if not data or "similartracks" not in data or "track" not in data["similartracks"]:
        return []

    out = []
    for t in data["similartracks"]["track"][:10]:
        out.append({
            "artist": t["artist"]["name"],
            "title": t.get("name"),
            "match": float(t.get("match", 0))
        })

    cache_put(key, out)
    return out


def lfm_artist_top_tracks(artist):
    key = f"lfm_top::{artist.lower()}"
    cached = cache_get(key)
    if cached:
        return cached

    url = (
        "https://ws.audioscrobbler.com/2.0/?method=artist.getTopTracks"
        f"&artist={artist}&limit=5&api_key={LASTFM_API_KEY}&format=json"
    )

    data = http_get_json(url)
    if not data or "toptracks" not in data or "track" not in data["toptracks"]:
        return []

    out = [t["name"] for t in data["toptracks"]["track"][:5]]

    cache_put(key, out)
    return out


# =====================================================================
#                    MASTER ENDPOINT
# =====================================================================

@app.route("/fourplay/metadata", methods=["POST"])
def metadata_endpoint():
    try:
        payload = request.json
        artist = payload.get("artist", "")
        title = payload.get("title", "")
        content_year = payload.get("contentYear", 0)

        if not artist or not title:
            return jsonify({"error": "Missing artist/title"}), 400

        # 1. MusicBrainz
        mb = mb_lookup_recording(artist, title)
        if mb:
            mb["eraScore"] = mb_era_score(mb.get("year"), content_year)

        # 2. ListenBrainz
        lb_sim = lb_fetch_similar(artist)
        lb_weight = lb_listener_weight(artist)

        lb = {
            "similarArtists": lb_sim,
            "overlap": 1 if lb_sim else 0,
            "listenerWeight": lb_weight
        }

        # 3. Discogs
        disc = {
            "styleScore": discogs_style_score(artist),
            "continuityScore": 0.6 if discogs_style_score(artist) > 0 else 0
        }

        # 4. Last.fm
        lfm_sim = lfm_similar_tracks(artist, title)
        lfm_top = lfm_artist_top_tracks(artist)

        lfm = {
            "similarTracks": lfm_sim,
            "artistTopTracks": lfm_top,
            "popularity": min(1, len(lfm_top) / 5.0)
        }

        # Final bundle
        out = {
            "artist": artist,
            "title": title,
            "candidateYear": payload.get("year", 0),
            "mb": mb,
            "lb": lb,
            "discogs": disc,
            "lfm": lfm,
            "genre": payload.get("genre", {}),
            "scene": payload.get("scene", {}),
            "error": None
        }

        return jsonify(out)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
