#!/usr/bin/env python3
"""
Uploads a file to the Gemini Files API and waits for it to become ACTIVE.
Uses only Python standard library (no external dependencies).
"""

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import urllib.error

def get_api_key(args):
    """Retrieves API key from command args or environment."""
    if args.api_key:
        return args.api_key
    return os.environ.get("GEMINI_API_KEY")

def detect_mime_type(file_path):
    """Determines MIME type based on file extension, falling back to standard mimetypes module."""
    ext = os.path.splitext(file_path)[1].lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }
    
    # Try custom map first for standard types we explicitly support
    if ext in mime_map:
        return mime_map[ext]
        
    # Fallback to standard library mimetypes module
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        return mime_type
        
    return "application/octet-stream"

def upload_file(file_path, display_name=None, api_key=None):
    """Performs a robust resumable upload to the Files API, with automatic pre-processing for large videos."""
    file_size = os.path.getsize(file_path)
    mime_type = detect_mime_type(file_path)
    
    # Large video file size check (>25MB)
    is_video = mime_type.startswith("video/")
    if is_video and file_size > 25 * 1024 * 1024:
        size_mb = file_size / (1024 * 1024)
        print(f"\nWARNING: Video file '{file_path}' is very large ({size_mb:.2f} MB)!")
        print("Note: Gemini Omni Flash is optimized for 10s videos at 720p and 24fps. Uploading very large or")
        print("high-resolution videos will significantly increase upload times and may cause Out-Of-Memory (OOM) errors.")
        
        # Determine if terminal is interactive
        if sys.stdin.isatty():
            print("\nWould you like to automatically pre-process this video first using prep_video.py?")
            print("This will trim, scale, and optimize the video to ensure a fast, OOM-safe upload.")
            try:
                choice = input("Pre-process video? [Y/n]: ").strip().lower()
                if choice in ("", "y", "yes"):
                    prepped_output_path = os.path.join("media", f"prepped_{os.path.basename(file_path)}")
                    os.makedirs("media", exist_ok=True)
                    
                    # Resolve prep_video.py script path
                    import subprocess
                    prep_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video", "prep_video.py")
                    if not os.path.exists(prep_script):
                        prep_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prep_video.py")
                        
                    cmd = [sys.executable, prep_script, file_path, "--output", prepped_output_path]
                    print(f"Running: {' '.join(cmd)}")
                    
                    try:
                        result = subprocess.run(cmd)
                        if result.returncode == 0 and os.path.exists(prepped_output_path):
                            file_path = prepped_output_path
                            file_size = os.path.getsize(file_path)
                            print(f"\nPre-processing completed successfully! Proceeding with upload of prepped video ({file_size / (1024*1024):.2f} MB)...")
                        else:
                            print("Error: Video pre-processing failed. Proceeding with original file upload is not recommended.", file=sys.stderr)
                            sys.exit(1)
                    except Exception as e:
                        print(f"Error executing prep_video.py: {e}", file=sys.stderr)
                        sys.exit(1)
                else:
                    proceed_choice = input("Do you want to proceed with uploading the original large video anyway? [y/N]: ").strip().lower()
                    if proceed_choice not in ("y", "yes"):
                        print("Upload cancelled by user. Please pre-process the video manually first.", file=sys.stderr)
                        sys.exit(1)
            except (KeyboardInterrupt, EOFError):
                print("\nNo input received. Upload cancelled to prevent OOM.", file=sys.stderr)
                sys.exit(1)
        else:
            # Non-interactive mode
            if file_size > 100 * 1024 * 1024: # Block files larger than 100MB in non-interactive mode
                print(f"Error: Video file is extremely large ({size_mb:.2f} MB) and script is running in non-interactive mode.", file=sys.stderr)
                print("To prevent Out-Of-Memory (OOM) errors, upload has been blocked.", file=sys.stderr)
                print("Please pre-process the video first using prep_video.py.", file=sys.stderr)
                sys.exit(1)
            else:
                print("Proceeding with upload in non-interactive mode...", file=sys.stderr)

    if not display_name:
        display_name = os.path.basename(file_path)

    print(f"Preparing upload of '{file_path}' ({file_size} bytes, type: {mime_type})...")

    # Step 1: Initialize Resumable Session
    url = "https://generativelanguage.googleapis.com/upload/v1beta/files"
    headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    body = json.dumps({"file": {"display_name": display_name}}).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=480) as resp:
            upload_url = resp.info().get("X-Goog-Upload-URL")
    except urllib.error.HTTPError as e:
        print(f"Error starting upload session: {e.code} - {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    if not upload_url:
        print("Error: No upload URL returned in the response headers.", file=sys.stderr)
        sys.exit(1)

    # Step 2: Upload Bytes
    print("Uploading file bytes...")
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    headers = {
        "Content-Length": str(file_size),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }

    req = urllib.request.Request(upload_url, data=file_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=480) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["file"]
    except urllib.error.HTTPError as e:
        print(f"Error uploading bytes: {e.code} - {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

def wait_for_active(file_name, api_key, poll_interval=3, max_attempts=30, backoff_factor=1.5, max_interval=30):
    """Polls the file status until state is ACTIVE or FAILED using exponential backoff."""
    url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
    print(f"Waiting for file {file_name} to finish processing...")
    
    attempt = 0
    current_interval = poll_interval
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    while attempt < max_attempts:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("x-goog-api-key", api_key)
            with urllib.request.urlopen(req, timeout=480) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                state = data.get("state", "UNKNOWN")
                
                # Reset consecutive errors on successful API response
                consecutive_errors = 0
                
                if state == "ACTIVE":
                    print("File is ACTIVE and ready for generations!")
                    return data
                elif state == "FAILED":
                    print(f"Error: File processing failed on the backend.", file=sys.stderr)
                    sys.exit(1)
                
                print(f"Current state: {state}. Retrying in {current_interval:.1f}s...")
                time.sleep(current_interval)
                
                # Increase interval for the next poll (backoff)
                current_interval = min(current_interval * backoff_factor, max_interval)
                attempt += 1
                
        except urllib.error.HTTPError as e:
            # Check for terminal HTTP errors
            if e.code in (400, 401, 403, 404):
                print(f"Terminal HTTP Error {e.code}: {e.read().decode()}", file=sys.stderr)
                sys.exit(1)
                
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                print(f"Error: Too many consecutive HTTP errors ({e.code}). Exiting.", file=sys.stderr)
                sys.exit(1)
                
            print(f"Warning: HTTP Error checking status ({e.code}). Retrying in {current_interval:.1f}s...")
            time.sleep(current_interval)
            current_interval = min(current_interval * backoff_factor, max_interval)
            attempt += 1
            
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                print(f"Error: Too many consecutive errors ({e}). Exiting.", file=sys.stderr)
                sys.exit(1)
                
            print(f"Warning: Error checking status ({e}). Retrying in {current_interval:.1f}s...")
            time.sleep(current_interval)
            current_interval = min(current_interval * backoff_factor, max_interval)
            attempt += 1
            
    print(f"Error: Maximum polling attempts ({max_attempts}) reached. File is still not ACTIVE.", file=sys.stderr)
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Upload files to Gemini Files API using REST.")
    parser.add_argument("file", help="Path to the file to upload")
    parser.add_argument("--name", help="Custom display name for the file")
    parser.add_argument("--api-key", help="Gemini API Key (overrides env)")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for ACTIVE status")
    
    args = parser.parse_args()
    
    api_key = get_api_key(args)
    if not api_key:
        print("Error: API key is not set. Use --api-key or set GEMINI_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.file):
        print(f"Error: File '{args.file}' not found.", file=sys.stderr)
        sys.exit(1)

    file_meta = upload_file(args.file, args.name, api_key)
    file_name = file_meta.get("name")
    
    print(f"File metadata created:")
    print(f"  Name: {file_name}")
    print(f"  URI:  {file_meta.get('uri')}")
    print(f"  Type: {file_meta.get('mimeType')}")

    if not args.no_wait:
        file_meta = wait_for_active(file_name, api_key)
        
    print("\nFile upload successfully completed! JSON Output:")
    print(json.dumps(file_meta, indent=2))

if __name__ == "__main__":
    main()
