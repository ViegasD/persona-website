import hashlib

def video_version(item):
    return hashlib.sha256(item.video_s3_key.encode()).hexdigest() if item.video_s3_key else None
