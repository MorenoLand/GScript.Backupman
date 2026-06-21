#!/usr/bin/env python3

import ctypes
import hashlib
import json
import os
import re
import time
import threading
import platform
import curses
from ctypes import PYFUNCTYPE, POINTER, Structure, c_char_p, c_int, c_void_p
from typing import Dict, List, Optional

class RCServer(Structure):
    _fields_ = [
        ("name", c_char_p),
        ("ip", c_char_p),
        ("port", c_int),
        ("players", c_int),
        ("language", c_char_p),
        ("description", c_char_p),
    ]

class RCFileBrowserFolder(Structure):
    _fields_ = [("rights", c_char_p), ("pattern", c_char_p)]

class RCFileBrowserEntry(Structure):
    _fields_ = [
        ("path", c_char_p),
        ("rights", c_char_p),
        ("size", c_int),
        ("modified", c_int),
        ("is_directory", c_int),
    ]

RC_OnConnected = PYFUNCTYPE(None, c_void_p)
RC_OnDisconnected = PYFUNCTYPE(None, c_char_p, c_void_p)
RC_OnMessage = PYFUNCTYPE(None, c_char_p, c_void_p)
RC_OnFileReceived = PYFUNCTYPE(None, c_char_p, c_void_p, c_int, c_void_p)
RC_OnFileBrowserFolders = PYFUNCTYPE(None, c_int, c_void_p)
RC_OnFileBrowserFiles = PYFUNCTYPE(None, c_char_p, c_int, c_void_p)
RC_OnFileBrowserMessage = PYFUNCTYPE(None, c_char_p, c_void_p)
RC_OnServerData = PYFUNCTYPE(None, c_char_p, c_char_p, c_void_p)

def sanitizePath(path: str) -> str:
    invalid_chars = r'[\\/:*?"<>|]'
    parts = path.split('/')
    sanitized_parts = [re.sub(invalid_chars, '_', part) for part in parts]
    return '/'.join(sanitized_parts)

def isZipPath(path: str) -> bool:
    return str(path or "").replace('\\', '/').rstrip('/').lower().endswith('.zip')

def stringHashcode(s: str) -> int:
    h = 0
    for char in s:
        h = (31 * h + ord(char)) & 0xFFFFFFFF
    return h if not (h & 0x80000000) else -((h ^ 0xFFFFFFFF) + 1)

def computerCode(computer_hash: int) -> str:
    result = []
    for _ in range(32):
        result.append("0123456789ABCDEF"[abs(computer_hash % 16)])
        computer_hash *= 31
    return ''.join(result)

def generateBackupPcidList(account: str) -> str:
    try:
        import urllib.request
        ip_hash = stringHashcode(urllib.request.urlopen('https://api.ipify.org', timeout=2).read().decode('utf-8'))
    except:
        ip_hash = stringHashcode(os.environ.get("COMPUTERNAME", ""))
    pcid = computerCode(ip_hash + stringHashcode(account))
    os_prefix = "win" if platform.system() == "Windows" else "mac"
    return '{},{},{},""'.format(os_prefix, pcid, pcid)

def getCleanServerName(server_name: str) -> str:
    cleaned = re.sub(r'^[^\w.\- ]+\s*', '', server_name)
    return sanitizePath(cleaned.strip())

class BackupBoi:
    def __init__(self):
        self.config = {}
        self.authenticated = False
        self.servers = []
        self.folders = []
        self.folder_files = []
        self.current_folder = ""
        self.pending_file_download: Optional[str] = None
        self.receive_thread = None
        self.running = False
        self.server_name = ""
        self.base_dir = ""
        self.downloaded_count = 0
        self.total_files = 0
        self.failed_count = 0
        self.last_processed_count = 0
        self.current_file = ""
        self.expecting_folder_contents = False
        self.folder_contents_received = threading.Event()
        self.initial_folder_received = False
        self.progress_line_active = False
        self.debug_mode = False
        self.debug_log_path = os.path.join(os.path.dirname(__file__), "debug.log")
        self.ui_active = False
        self.ui_thread = None
        self.ui_lock = threading.Lock()
        self.server_options = None
        self.server_flags = None
        self.folder_config = None
        self.pending_server_options = threading.Event()
        self.pending_server_flags = threading.Event()
        self.pending_folder_config = threading.Event()
        self.disconnected = False
        self.disconnect_reason = ""
        self.ui_stats = {
            'current_file': '',
            'current_file_size': 0,
            'current_file_received': 0,
            'downloaded_count': 0,
            'failed_count': 0,
            'processed_count': 0,
            'total_files': 0,
            'current_folder': '',
            'status': 'Initializing...',
            'start_time': 0,
            'download_start_time': 0,
            'download_start_count': 0,
            'download_start_bytes': 0,
            'bytes_downloaded': 0,
            'total_size': 0,
            'folders_scanned': 0,
            'servers': [],
            'selected_server': None,
            'selected_index': 0,
            'phase': 'init',
            'server_selected': False
        }
        self.folder_index = {}
        self.scan_mode = False
        
    def log(self, message: str, debug_only: bool = False):
        if debug_only:
            if not self.debug_mode:
                return
            try:
                with open(self.debug_log_path, 'a', encoding='utf-8') as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
            except:
                pass
        else:
            if self.debug_mode:
                try:
                    with open(self.debug_log_path, 'a', encoding='utf-8') as f:
                        f.write(f"[{time.strftime('%H:%M:%S')}] [LOG] {message}\n")
                except:
                    pass
        if not self.ui_active:
            print(f"[BackupBoi] {message}")
        else:
            with self.ui_lock:
                if not debug_only:
                    self.ui_stats['status'] = message
    
    def update_progress(self, current_file: str = "", size_info: str = ""):
        with self.ui_lock:
            if current_file:
                self.ui_stats['current_file'] = os.path.basename(current_file)
                self.ui_stats['status'] = f"Downloading: {os.path.basename(current_file)}"
                if size_info:
                    self.ui_stats['status'] += f" {size_info}"
            self.ui_stats['downloaded_count'] = self.downloaded_count
            self.ui_stats['total_files'] = self.total_files
            self.ui_stats['current_folder'] = self.current_folder
    
    def load_config(self) -> bool:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if not os.path.exists(config_path):
            self.log(f"Config file not found: {config_path}")
            self.log("Creating sample config.json...")
            sample_config = {
                "listserver_name": "Retail",
                "listserver_host": "listserver.graalonline.com",
                "listserver_port": 14922,
                "listserver_account": "YourAccount",
                "listserver_password": "YourPassword",
                "debug_mode": False,
                "force_rescan": False,
                "skip_folders": [],
                "only_download_enabled": False,
                "only_download_folders": []
            }
            with open(config_path, 'w') as f:
                json.dump(sample_config, f, indent=2)
            self.log("Please edit config.json with your credentials and run again")
            return False
        
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        self.debug_mode = self.config.get("debug_mode", False)
        self.force_rescan = bool(self.config.get("force_rescan", False))
        self.skip_folders = self.config.get("skip_folders", [])
        if isinstance(self.skip_folders, str):
            self.skip_folders = [self.skip_folders]
        self.only_download_enabled = bool(self.config.get("only_download_enabled", False))
        self.only_download_folders = self.config.get("only_download_folders", [])
        if isinstance(self.only_download_folders, str):
            self.only_download_folders = [self.only_download_folders]
        self.only_download_folders = [
            self._normalize_folder_filter(folder)
            for folder in self.only_download_folders
            if str(folder).strip()
        ]
        if self.only_download_enabled and not self.only_download_folders:
            self.log("only_download_enabled is true but only_download_folders is empty; backing up all non-skipped folders")
        
        if self.debug_mode:
            if os.path.exists(self.debug_log_path):
                with open(self.debug_log_path, 'w') as f:
                    f.write("")
        
        return True

    def _normalize_folder_filter(self, folder_path: str) -> str:
        return str(folder_path).replace('\\', '/').strip('/').lower()

    def is_allowed_by_only_download(self, path: str) -> bool:
        if not self.only_download_enabled or not self.only_download_folders:
            return True
        path_norm = self._normalize_folder_filter(path)
        if not path_norm:
            return True
        for root in self.only_download_folders:
            if path_norm == root or path_norm.startswith(root + '/') or root.startswith(path_norm + '/'):
                return True
        return False

    def should_process_folder(self, folder_path: str) -> bool:
        return not self.should_skip_folder(folder_path) and self.is_allowed_by_only_download(folder_path)

    def should_process_file(self, file_path: str) -> bool:
        if not self.is_allowed_by_only_download(file_path):
            return False
        folder_path = os.path.dirname(file_path).replace('\\', '/') or '/'
        return not self.should_skip_folder(folder_path)
    
    def get_listserver_config(self) -> Dict:
        config = {
            "name": self.config.get("listserver_name", "Listserver"),
            "host": self.config.get("listserver_host", "127.0.0.1"),
            "port": self.config.get("listserver_port", 14922),
            "account": self.config.get("listserver_account", ""),
            "password": self.config.get("listserver_password", "")
        }
        return config
    
    def should_skip_folder(self, folder_path: str) -> bool:
        if not self.skip_folders:
            return False
        folder_path_norm = folder_path.replace('\\', '/').strip('/').lower()
        for skip_pattern in self.skip_folders:
            skip_pattern_norm = str(skip_pattern).replace('\\', '/').strip('/').lower()
            if not skip_pattern_norm:
                continue
            if folder_path_norm == skip_pattern_norm or folder_path_norm.startswith(skip_pattern_norm + '/'):
                return True
        return False
    
    def save_server_configs(self):
        if not self.base_dir:
            return
        
        if self.server_options is not None:
            config_path = os.path.join(self.base_dir, "serveroptions.txt")
            try:
                _backupman_write_bytes_atomic(config_path, self.server_options.encode('utf-8'))
                self.log(f"Saved server options to {os.path.basename(config_path)}")
            except Exception as e:
                self.log(f"Failed to save server options: {str(e)}")
        
        if self.server_flags is not None:
            config_path = os.path.join(self.base_dir, "serverflags.txt")
            try:
                _backupman_write_bytes_atomic(config_path, self.server_flags.encode('utf-8'))
                self.log(f"Saved server flags to {os.path.basename(config_path)}")
            except Exception as e:
                self.log(f"Failed to save server flags: {str(e)}")
        
        if self.folder_config is not None:
            config_path = os.path.join(self.base_dir, "folderconfig.txt")
            try:
                _backupman_write_bytes_atomic(config_path, self.folder_config.encode('utf-8'))
                self.log(f"Saved folder config to {os.path.basename(config_path)}")
            except Exception as e:
                self.log(f"Failed to save folder config: {str(e)}")

    def write_backup_manifest(self, file_list, skipped_count, failed_files):
        manifest_path = os.path.join(self.base_dir, "backup_manifest.json")
        failed_paths = sorted({item.get('path', '') for item in failed_files if item.get('path')})
        files = []
        present_count = 0
        present_bytes = 0
        for file_info in file_list:
            file_path = file_info.get('path', '')
            if not file_path or not self.should_process_file(file_path):
                continue
            try:
                local_file_path = _backupman_local_path(self.base_dir, file_path)
            except Exception as e:
                files.append({
                    'path': file_path,
                    'expected_size': file_info.get('size', 0),
                    'present': False,
                    'error': str(e),
                })
                continue
            entry = {
                'path': file_path,
                'expected_size': file_info.get('size', 0),
                'present': os.path.exists(local_file_path),
            }
            if entry['present']:
                size = os.path.getsize(local_file_path)
                present_count += 1
                present_bytes += size
                entry['actual_size'] = size
                entry['sha256'] = self._sha256_file(local_file_path)
            files.append(entry)
        manifest = {
            'server': self.server_name,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'source': 'GRCLib',
            'stats': {
                'indexed_files': len(file_list),
                'present_files': present_count,
                'present_bytes': present_bytes,
                'skipped_existing': skipped_count,
                'failed_files': len(failed_paths),
            },
            'failed_files': failed_paths,
            'files': files,
        }
        try:
            _backupman_write_json_atomic(manifest_path, manifest)
            self.log(f"Backup manifest saved to {os.path.basename(manifest_path)}")
        except Exception as e:
            self.log(f"Failed to write backup manifest: {str(e)}")

    def _sha256_file(self, path):
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    
    def scan_recursive(self, folder_path: str = ""):
        if not self.should_process_folder(folder_path):
            self.log(f"Skipping folder (filtered): {folder_path}", debug_only=True)
            return
        
        if not folder_path:
            self.log("Scanning from root...")
            if not self.change_folder(""):
                self.log(f"  Failed to change to root folder")
                return
        else:
            self.log(f"Scanning folder: {folder_path}")
            with self.ui_lock:
                self.ui_stats['current_folder'] = folder_path
                self.ui_stats['status'] = f"Scanning folder: {folder_path}"
                self.ui_stats['folders_scanned'] += 1
            if not self.change_folder(folder_path):
                self.log(f"  Failed to change to folder: {folder_path}")
                return
        
        folder_key = folder_path if folder_path else "/"
        if folder_key in self.folder_index:
            self.log(f"  Already scanned {folder_path}, skipping", debug_only=True)
            return
        
        self.folder_index[folder_key] = {
            'files': [],
            'subdirs': []
        }
        
        subdirs = []
        files = []
        
        self.log(f"Processing {len(self.folder_files)} items in folder: {folder_path}")
        dir_count = 0
        file_count = 0
        for item in self.folder_files:
            filename = item['path']
            rights = item['rights']
            is_dir = item['is_directory']
            self.log(f"  Item: '{filename}' | rights='{rights}' | is_dir={is_dir} | ends_with_slash={filename.endswith('/')}", debug_only=True)
            if is_dir and not isZipPath(filename):
                dir_count += 1
                self.log(f"  Directory: {filename} (rights: {rights})")
                subdirs.append(filename)
            else:
                file_count += 1
                if 'r' in rights or isZipPath(filename):
                    if folder_path:
                        folder_path_clean = folder_path.rstrip('/')
                        full_path = f"{folder_path_clean}/{filename}"
                    else:
                        full_path = filename
                    files.append({
                        'path': full_path,
                        'size': item.get('size', 0),
                        'filename': filename,
                        'rights': rights
                    })
                if file_count <= 5:
                    self.log(f"  File: {filename} (rights: {rights}, size: {item['size']})", debug_only=True)
        
        if file_count > 5:
            self.log(f"  ... and {file_count - 5} more files", debug_only=True)
        
        self.folder_index[folder_key]['subdirs'] = subdirs
        self.folder_index[folder_key]['files'] = files
        
        self.log(f"  Found {len(subdirs)} subdirectories, {len(files)} readable files in {folder_path}")
        if len(subdirs) == 0 and len(self.folder_files) > 10:
            self.log(f"  WARNING: No subdirectories found in folder with {len(self.folder_files)} items - checking first 10 items:")
            for i, item in enumerate(self.folder_files[:10]):
                self.log(f"    [{i}] path='{item['path']}' rights='{item['rights']}' is_dir={item['is_directory']}")
        
        for subdir in subdirs:
            if folder_path:
                folder_path_clean = folder_path.rstrip('/')
                new_folder_path = f"{folder_path_clean}/{subdir}"
            else:
                new_folder_path = subdir
            if self.should_process_folder(new_folder_path):
                self.log(f"Recursing into subdirectory: {new_folder_path}")
                self.scan_recursive(new_folder_path)
            else:
                self.log(f"Skipping subdirectory (filtered): {new_folder_path}", debug_only=True)
    
    def download_from_index(self):
        file_index_file = os.path.join(self.base_dir, "file_index.json")
        if not os.path.exists(file_index_file):
            self.log(f"File index not found: {file_index_file}")
            return 0
        
        with open(file_index_file, 'r', encoding='utf-8') as f:
            file_index_data = json.load(f)
        
        file_list = file_index_data.get('files', [])
        
        already_downloaded = 0
        already_downloaded_bytes = 0
        files_in_index_set = set()
        zip_files_in_index = set()
        already_counted_files = set()
        for file_info in file_list:
            file_path = file_info['path']
            files_in_index_set.add(file_path)
            if file_path.endswith('.zip'):
                zip_files_in_index.add(file_path)
            expected_size = file_info['size']
            local_file_path = _backupman_local_path(self.base_dir, file_path)
            if os.path.exists(local_file_path):
                existing_size = os.path.getsize(local_file_path)
                if expected_size > 0 and existing_size == expected_size:
                    already_downloaded += 1
                    already_downloaded_bytes += existing_size
                    already_counted_files.add(file_path)
        
        extra_files_on_disk = 0
        extra_bytes_on_disk = 0
        try:
            for root, dirs, files in os.walk(self.base_dir):
                if 'file_index.json' in files or 'folder_cache.json' in files:
                    continue
                for file in files:
                    if file in ('backup_manifest.json', 'serveroptions.txt', 'serverflags.txt', 'folderconfig.txt') or '.part.' in file:
                        continue
                    rel_path = os.path.relpath(os.path.join(root, file), self.base_dir).replace('\\', '/')
                    if not self.should_process_file(rel_path):
                        continue
                    if rel_path not in files_in_index_set:
                        extra_files_on_disk += 1
                        try:
                            file_path = os.path.join(root, file)
                            extra_bytes_on_disk += os.path.getsize(file_path)
                        except:
                            pass
        except:
            pass
        
        if extra_files_on_disk > 0:
            self.log(f"Found {extra_files_on_disk} files on disk not in current index (from previous sessions)", debug_only=True)
            self.log(f"Extra files size: {self._format_bytes(extra_bytes_on_disk)}", debug_only=True)
            already_downloaded = already_downloaded + extra_files_on_disk
            already_downloaded_bytes = already_downloaded_bytes + extra_bytes_on_disk
        
        files_by_folder = {}
        actual_files_to_process = 0
        actual_total_size = 0
        for file_info in file_list:
            file_path = file_info['path']
            folder_path = os.path.dirname(file_path).replace('\\', '/')
            if not folder_path:
                folder_path = '/'
            if not self.should_process_file(file_path):
                continue
            if folder_path not in files_by_folder:
                files_by_folder[folder_path] = []
            files_by_folder[folder_path].append(file_info)
            actual_files_to_process += 1
            actual_total_size += file_info.get('size', 0)
        
        total_files = actual_files_to_process + extra_files_on_disk
        total_size = actual_total_size + extra_bytes_on_disk
        self.downloaded_count = already_downloaded
        
        self.log(f"Starting download of {total_files} files ({total_size:,} bytes total)...")
        self.log(f"Found {already_downloaded} files already downloaded from previous sessions")
        self.log(f"Total files to process: {actual_files_to_process} in index (after skip_folders) + {extra_files_on_disk} extra on disk = {total_files} total", debug_only=True)
        self.log(f"Total size: {actual_total_size:,} from index + {extra_bytes_on_disk:,} extra = {total_size:,} total", debug_only=True)
        with self.ui_lock:
            self.ui_stats['total_files'] = total_files
            self.ui_stats['total_size'] = total_size
            self.ui_stats['downloaded_count'] = already_downloaded
            self.ui_stats['bytes_downloaded'] = already_downloaded_bytes
            self.ui_stats['download_start_time'] = time.time()
            self.ui_stats['download_start_count'] = already_downloaded
            self.ui_stats['download_start_bytes'] = already_downloaded_bytes
            self.ui_stats['status'] = f'Downloading files...'
        self.log(f"UI stats: total_files={total_files}, downloaded_count={already_downloaded}", debug_only=True)
        
        skipped_count = 0
        failed_count = 0
        stale_count = 0
        current_folder = None
        files_processed = set()
        failed_files = []
        
        MAX_RETRIES = 3
        MAX_LOOP_RETRIES = 3
        
        for folder_path in sorted(files_by_folder.keys()):
            folder_display = folder_path if folder_path != '/' else 'root'
            if folder_path != current_folder:
                folder_change_success = False
                for retry in range(MAX_RETRIES):
                    if folder_path == '/':
                        folder_display = 'root'
                        if self.change_folder(""):
                            folder_change_success = True
                            break
                    else:
                        folder_display = folder_path
                        if self.change_folder(folder_path + '/'):
                            folder_change_success = True
                            break
                    self.log(f"Failed to change to folder: {folder_path} (attempt {retry+1}/{MAX_RETRIES}), retrying...")
                    time.sleep(0.5)
                
                if not folder_change_success:
                    self.log(f"Failed to change to folder after {MAX_RETRIES} attempts: {folder_path}")
                    continue
                current_folder = folder_path
                with self.ui_lock:
                    self.ui_stats['current_folder'] = folder_display
                    self.ui_stats['status'] = f"Downloading from: {folder_display}"

            live_names = {item.get('path') for item in self.folder_files if not item.get('is_directory')}
            
            for file_info in files_by_folder[folder_path]:
                try:
                    file_path = file_info['path']
                    if file_path in files_processed:
                        continue
                    files_processed.add(file_path)
                    basename = os.path.basename(file_path.replace('\\', '/'))
                    if live_names and basename not in live_names:
                        stale_count += 1
                        failed_files.append(file_info)
                        self.log(f"Stale index entry, file not in live folder listing: {file_path}")
                        continue
                    
                    if folder_path.endswith('.zip/') or folder_path.endswith('.zip'):
                        zip_path = folder_path.rstrip('/')
                        if zip_path not in zip_files_in_index:
                            self.log(f"File inside zip folder (zip not in file index, treating as folder only): {file_path}", debug_only=True)
                    
                    expected_size = file_info['size']
                    local_file_path = _backupman_local_path(self.base_dir, file_path)
                    
                    if os.path.exists(local_file_path):
                        existing_size = os.path.getsize(local_file_path)
                        if expected_size > 0 and existing_size == expected_size:
                            skipped_count += 1
                            if file_path not in already_counted_files:
                                self.downloaded_count += 1
                                with self.ui_lock:
                                    self.ui_stats['downloaded_count'] = self.downloaded_count
                                    self.ui_stats['bytes_downloaded'] += existing_size
                            continue
                    
                    with self.ui_lock:
                        self.ui_stats['current_file'] = os.path.basename(file_path)
                        self.ui_stats['current_file_size'] = expected_size
                        self.ui_stats['current_file_received'] = 0
                        self.ui_stats['status'] = f"Downloading: {os.path.basename(file_path)}"
                    
                    download_success = False
                    for retry in range(MAX_RETRIES):
                        if self.download_file(file_path):
                            download_success = True
                            break
                        self.log(f"Download failed: {file_path} (attempt {retry+1}/{MAX_RETRIES}), retrying...")
                        time.sleep(0.2)
                    
                    if download_success:
                        self.downloaded_count += 1
                        if self.downloaded_count % 10 == 0:
                            with self.ui_lock:
                                self.ui_stats['downloaded_count'] = self.downloaded_count
                    else:
                        failed_count += 1
                        failed_files.append(file_info)
                        self.log(f"Download failed after {MAX_RETRIES} attempts: {file_path}")
                        if os.path.exists(local_file_path):
                            existing_size = os.path.getsize(local_file_path)
                            self.log(f"  File exists but size mismatch: expected={expected_size}, actual={existing_size}", debug_only=True)
                    time.sleep(0.001)
                except Exception as e:
                    failed_count += 1
                    file_path_str = file_info.get('path', 'unknown') if 'file_info' in locals() else 'unknown'
                    self.log(f"Exception processing file {file_path_str}: {str(e)}")
                    import traceback
                    self.log(traceback.format_exc(), debug_only=True)
                    failed_files.append(file_info)
                    continue
        
        for retry_pass in range(1, MAX_LOOP_RETRIES):
            if not failed_files:
                break
            self.log(f"Retry pass {retry_pass}/{MAX_LOOP_RETRIES - 1}: re-processing {len(failed_files)} failed files...")
            with self.ui_lock:
                self.ui_stats['status'] = f'Retry pass {retry_pass}: {len(failed_files)} failed files'
            second_pass_failed = []
            for file_info in failed_files:
                file_path = file_info['path']
                folder_path = os.path.dirname(file_path).replace('\\', '/')
                if not folder_path:
                    folder_path = '/'
                
                if folder_path != current_folder:
                    folder_change_success = False
                    for retry in range(MAX_RETRIES):
                        if folder_path == '/':
                            if self.change_folder(""):
                                folder_change_success = True
                                break
                        else:
                            if self.change_folder(folder_path + '/'):
                                folder_change_success = True
                                break
                        time.sleep(0.5)
                    
                    if not folder_change_success:
                        self.log(f"Failed to change to folder on retry pass {retry_pass}: {folder_path}")
                        second_pass_failed.append(file_info)
                        continue
                    current_folder = folder_path
                    live_names = {item.get('path') for item in self.folder_files if not item.get('is_directory')}
                
                expected_size = file_info['size']
                local_file_path = _backupman_local_path(self.base_dir, file_path)
                basename = os.path.basename(file_path.replace('\\', '/'))
                if live_names and basename not in live_names:
                    second_pass_failed.append(file_info)
                    self.log(f"Retry pass {retry_pass}: stale index entry still missing from live folder listing: {file_path}")
                    continue
                
                if os.path.exists(local_file_path):
                    existing_size = os.path.getsize(local_file_path)
                    if expected_size > 0 and existing_size == expected_size:
                        self.downloaded_count += 1
                        self.log(f"Retry pass {retry_pass}: file now exists with correct size: {file_path}")
                        continue
                
                download_success = False
                for retry in range(MAX_RETRIES):
                    if self.download_file(file_path):
                        download_success = True
                        break
                    self.log(f"Retry pass {retry_pass} download failed: {file_path} (attempt {retry+1}/{MAX_RETRIES})")
                    time.sleep(0.2)
                
                if download_success:
                    self.downloaded_count += 1
                    self.log(f"Retry pass {retry_pass} success: {file_path}")
                else:
                    second_pass_failed.append(file_info)
                    self.log(f"Retry pass {retry_pass} FAILED after all retries: {file_path}")
            
            failed_count = len(second_pass_failed)
            failed_files = second_pass_failed
        
        present_indexed_count = 0
        for file_info in file_list:
            file_path = file_info.get('path', '')
            if not self.should_process_file(file_path):
                continue
            try:
                local_file_path = _backupman_local_path(self.base_dir, file_path)
                if os.path.exists(local_file_path):
                    expected_size = int(file_info.get('size') or 0)
                    actual_size = os.path.getsize(local_file_path)
                    if expected_size <= 0 or actual_size == expected_size:
                        present_indexed_count += 1
            except Exception as e:
                self.log(f"Failed to verify indexed file {file_path}: {str(e)}", debug_only=True)

        total_processed = present_indexed_count + failed_count
        expected_total = actual_files_to_process
        self.downloaded_count = present_indexed_count
        with self.ui_lock:
            self.ui_stats['downloaded_count'] = self.downloaded_count
            self.ui_stats['failed_count'] = failed_count
            self.ui_stats['processed_count'] = total_processed
            self.ui_stats['current_file'] = ''
            self.ui_stats['current_file_size'] = 0
            self.ui_stats['current_file_received'] = 0
        self.failed_count = failed_count
        self.last_processed_count = total_processed
        
        if skipped_count > 0:
            self.log(f"Skipped {skipped_count} already downloaded files")
        if failed_count > 0:
            self.log(f"Failed to download {failed_count} files")
        if stale_count > 0:
            self.log(f"Detected {stale_count} stale file-index entries missing from live folder listings")
        
        self.log(f"Download phase complete: {total_processed} files processed out of {expected_total} expected (downloaded: {self.downloaded_count}, skipped: {skipped_count}, failed: {failed_count})")
        if total_processed < expected_total:
            self.log(f"WARNING: Loop finished early! Only processed {total_processed}/{expected_total} files. Missing {expected_total - total_processed} files.")
        self.write_backup_manifest(file_list, skipped_count, failed_files)
        return total_processed
    
    def backup_recursive(self, folder_path: str = ""):
        if not self.should_process_folder(folder_path):
            self.log(f"Skipping folder (filtered): {folder_path}", debug_only=True)
            return

        if not folder_path:
            self.log("Starting backup from root...")
            if not self.change_folder(""):
                self.log(f"  Failed to change to root folder")
                return
        else:
            self.log(f"Processing folder: {folder_path}")
            with self.ui_lock:
                self.ui_stats['current_folder'] = folder_path
                self.ui_stats['status'] = f"Processing folder: {folder_path}"
                self.ui_stats['folders_scanned'] += 1
            if not self.change_folder(folder_path):
                self.log(f"  Failed to change to folder: {folder_path}")
                return
        
        local_folder_path = _backupman_local_path(self.base_dir, folder_path) if folder_path else self.base_dir
        os.makedirs(local_folder_path, exist_ok=True)
        
        files_to_download = []
        subdirs = []
        
        self.log(f"Processing {len(self.folder_files)} items in folder: {folder_path}", debug_only=True)
        for item in self.folder_files:
            if item['is_directory'] and not isZipPath(item['path']):
                self.log(f"Found subdirectory: {item['path']}", debug_only=True)
                subdirs.append(item['path'])
            else:
                if 'r' in item['rights'] or isZipPath(item['path']):
                    if folder_path:
                        folder_path_clean = folder_path.rstrip('/')
                        full_path = f"{folder_path_clean}/{item['path']}"
                    else:
                        full_path = item['path']
                    files_to_download.append({
                        'path': full_path,
                        'size': item.get('size', 0),
                        'filename': item['path']
                    })
                else:
                    self.log(f"Skipping file without read rights: {item['path']}", debug_only=True)
        
        self.log(f"  Found {len(files_to_download)} files, {len(subdirs)} subdirectories")
        
        skipped_count = 0
        for file_info in files_to_download:
            file_path = file_info['path']
            expected_size = file_info['size']
            local_file_path = _backupman_local_path(self.base_dir, file_path)
            
            self.log(f"Checking file: {file_path} (expected size: {expected_size})", debug_only=True)
            
            if os.path.exists(local_file_path):
                existing_size = os.path.getsize(local_file_path)
                self.log(f"File exists: {local_file_path} (existing: {existing_size}, expected: {expected_size})", debug_only=True)
                if expected_size > 0 and existing_size == expected_size:
                    self.log(f"Skipping already downloaded file: {file_path}", debug_only=True)
                    skipped_count += 1
                    continue
                else:
                    self.log(f"File size mismatch, re-downloading: {file_path} (existing: {existing_size}, expected: {expected_size})", debug_only=True)
            else:
                self.log(f"File does not exist, downloading: {file_path}", debug_only=True)
            
            self.log(f"Starting download: {file_path}", debug_only=True)
            if self.download_file(file_path):
                self.log(f"Download completed: {file_path}", debug_only=True)
            else:
                self.log(f"Download failed: {file_path}", debug_only=True)
            time.sleep(0.1)
        
        if skipped_count > 0:
            self.log(f"  Skipped {skipped_count} already downloaded files")
        
        self.log(f"Processing {len(subdirs)} subdirectories...", debug_only=True)
        for subdir in subdirs:
            if folder_path:
                folder_path_clean = folder_path.rstrip('/')
                new_folder_path = f"{folder_path_clean}/{subdir}"
            else:
                new_folder_path = subdir
            if not self.should_process_folder(new_folder_path):
                self.log(f"Skipping subdirectory (filtered): {new_folder_path}", debug_only=True)
                continue
            self.log(f"Recursing into subdirectory: {new_folder_path}", debug_only=True)
            self.backup_recursive(new_folder_path)
    
    def run(self):
        self.ui_stats['start_time'] = time.time()
        self.ui_stats['phase'] = 'init'
        self.ui_active = True
        self.ui_thread = threading.Thread(target=self._ui_loop, daemon=True)
        self.ui_thread.start()
        time.sleep(0.2)
        
        if not self.load_config():
            self.ui_active = False
            return
        
        with self.ui_lock:
            self.ui_stats['phase'] = 'listserver'
            self.ui_stats['status'] = 'Connecting to listserver...'
        
        listserver_config = self.get_listserver_config()
        servers = self.fetch_server_list(listserver_config)
        
        if not servers:
            with self.ui_lock:
                self.ui_stats['status'] = 'No servers found'
            time.sleep(2)
            self.ui_active = False
            return
        
        with self.ui_lock:
            self.ui_stats['phase'] = 'select'
            self.ui_stats['servers'] = servers
            self.ui_stats['selected_index'] = 0
            self.ui_stats['status'] = f'Found {len(servers)} servers - Use Up/Down to select, Enter to confirm'
            self.ui_stats['server_selected'] = False
        
        while not self.ui_stats.get('server_selected', False):
            time.sleep(0.05)
        
        with self.ui_lock:
            server_index = self.ui_stats.get('selected_index', 0)
        
        selected_server = servers[server_index]
        self.server_name = getCleanServerName(selected_server['name'])
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = os.path.join(script_dir, "Servers", self.server_name)
        os.makedirs(self.base_dir, exist_ok=True)
        
        with self.ui_lock:
            self.ui_stats['phase'] = 'connect'
            self.ui_stats['selected_server'] = selected_server
            self.ui_stats['status'] = f"Connecting to {selected_server['name']}..."
        
        if not self.connect_game_server(selected_server):
            with self.ui_lock:
                self.ui_stats['status'] = 'Failed to connect'
            time.sleep(2)
            self.ui_active = False
            return
        
        with self.ui_lock:
            self.ui_stats['status'] = 'Requesting server configs...'
        
        if not self.request_server_options() or not self.running:
            self.ui_active = False
            return
        if not self.request_server_flags() or not self.running:
            self.ui_active = False
            return
        if not self.request_folder_config() or not self.running:
            self.ui_active = False
            return
        self.save_server_configs()
        
        time.sleep(1)
        
        with self.ui_lock:
            self.ui_stats['status'] = 'Opening file browser...'
        
        if not self.open_file_browser():
            with self.ui_lock:
                self.ui_stats['status'] = 'Failed to open file browser'
            time.sleep(2)
            self.ui_active = False
            return
        
        time.sleep(3)
        
        with self.ui_lock:
            self.ui_stats['phase'] = 'backup'
            self.ui_stats['status'] = 'Starting backup...'
        
        try:
            folder_cache_file = os.path.join(self.base_dir, "folder_cache.json")
            file_index_file = os.path.join(self.base_dir, "file_index.json")
            total_processed = 0
            total_files = 0
            
            if os.path.exists(folder_cache_file) and os.path.exists(file_index_file) and not self.force_rescan:
                self.log("Folder cache and file index already exist, skipping scan phase")
                self.log("Phase 2: Downloading files from existing index...")
                self.scan_mode = False
                total_processed = self.download_from_index() or 0
            else:
                if self.force_rescan and (os.path.exists(folder_cache_file) or os.path.exists(file_index_file)):
                    self.log("force_rescan enabled; rebuilding folder cache and file index")
                self.log(f"Received {len(self.folders)} folder patterns from server")
                self.log(f"Initial folder: {self.current_folder}")
                self.log(f"Initial folder has {len(self.folder_files)} items")
                
                self.log("Phase 1: Scanning all folders to build index...")
                self.scan_mode = True
                
                if hasattr(self, 'all_folder_patterns') and self.all_folder_patterns:
                    self.log(f"Extracting all folder paths from {len(self.all_folder_patterns)} folder patterns...")
                    all_folders = set()
                    def add_scan_folder(path):
                        if not path:
                            return
                        if not path.endswith('/'):
                            path = path + '/'
                        if not self.should_process_folder(path):
                            self.log(f"Not queuing filtered folder for scan: {path}", debug_only=True)
                            return
                        all_folders.add(path)

                    for folder_info in self.all_folder_patterns:
                        pattern = folder_info['pattern']
                        if 'r' in folder_info['rights'] or 'd' in folder_info['rights']:
                            if '*' in pattern or '?' in pattern:
                                folder_path = pattern.split('*')[0].split('?')[0]
                                add_scan_folder(folder_path)
                                parts = folder_path.rstrip('/').split('/') if folder_path else []
                                for i in range(1, len(parts)):
                                    parent_path = '/'.join(parts[:i]) + '/'
                                    add_scan_folder(parent_path)
                            else:
                                add_scan_folder(pattern)
                                parts = pattern.rstrip('/').split('/')
                                for i in range(1, len(parts)):
                                    parent_path = '/'.join(parts[:i]) + '/'
                                    add_scan_folder(parent_path)
                    
                    self.log(f"Extracted {len(all_folders)} unique folders from patterns (no hardcoded names)")
                    self.log(f"Sample folders: {sorted(list(all_folders))[:10]}", debug_only=True)
                    for folder_path in sorted(all_folders):
                        self.scan_recursive(folder_path)
                elif self.current_folder and self.folder_files:
                    self.log(f"Starting scan from initial folder: {self.current_folder}")
                    self.scan_recursive(self.current_folder)
                elif self.current_folder and self.folder_files:
                    parent_folder = '/'.join(self.current_folder.rstrip('/').split('/')[:-1])
                    if parent_folder:
                        self.log(f"Navigating to parent folder: {parent_folder}/")
                        if self.change_folder(parent_folder + '/'):
                            self.log(f"Successfully navigated to parent, starting scan")
                            self.scan_recursive(parent_folder + '/')
                        else:
                            self.log(f"Failed to navigate to parent, starting from initial folder")
                            self.scan_recursive(self.current_folder)
                    else:
                        self.log(f"Starting scan from initial folder: {self.current_folder}")
                        self.scan_recursive(self.current_folder)
                else:
                    self.log("No initial folder contents available, cannot start scan")
                    return
                
                self.log(f"Scan complete! Found {len(self.folder_index)} folders")
                total_subdirs = sum(len(folder['subdirs']) for folder in self.folder_index.values())
                total_files = sum(len(folder['files']) for folder in self.folder_index.values())
                self.log(f"Total folders discovered: {len(self.folder_index)}")
                self.log(f"Total subdirectories found: {total_subdirs}")
                self.log(f"Total files found: {total_files}")
                
                all_folders = sorted(self.folder_index.keys())
                cache_file = os.path.join(self.base_dir, "folder_cache.json")
                file_index_file = os.path.join(self.base_dir, "file_index.json")
                
                cache_data = {
                    'folders': all_folders,
                    'stats': {
                        'total_folders': len(self.folder_index),
                        'total_subdirs': total_subdirs,
                        'total_files': total_files
                    }
                }
                _backupman_write_json_atomic(cache_file, cache_data)
                self.log(f"Folder cache saved to: {os.path.relpath(cache_file, os.path.dirname(__file__))}")
                
                file_list = []
                for folder_path, folder_data in sorted(self.folder_index.items()):
                    for file_info in folder_data['files']:
                        file_list.append({
                            'path': file_info['path'],
                            'size': file_info['size']
                        })
                
                file_index_data = {
                    'files': file_list,
                    'stats': {
                        'total_files': len(file_list),
                        'total_size': sum(f['size'] for f in file_list)
                    }
                }
                _backupman_write_json_atomic(file_index_file, file_index_data)
                self.log(f"File index saved to: {os.path.relpath(file_index_file, os.path.dirname(__file__))} ({len(file_list)} files)")
                self.log(f"Found {len(all_folders)} unique folders, {total_files} files to download")
                
                with self.ui_lock:
                    self.ui_stats['status'] = f'Scan complete! {len(all_folders)} folders, {total_files} files'
                    self.ui_stats['total_files'] = total_files
                self.log("Scan phase complete, starting download phase...")
                
                self.scan_mode = False
                total_processed = self.download_from_index() or 0

            total_files = self.ui_stats.get('total_files', 0)
            failed_count = int(self.ui_stats.get('failed_count') or 0)
            if total_processed < total_files or failed_count > 0:
                self.log(f"WARNING: Backup incomplete: processed {total_processed}/{total_files}, failed {failed_count}, saved {self.downloaded_count}.")
                with self.ui_lock:
                    self.ui_stats['status'] = f'Incomplete: {total_processed}/{total_files} processed, {failed_count} failed - resume to retry'
                    self.ui_stats['current_file'] = ''
                    self.ui_stats['current_file_size'] = 0
                    self.ui_stats['current_file_received'] = 0
            else:
                with self.ui_lock:
                    self.ui_stats['status'] = f'Backup complete! {self.downloaded_count} files saved'
                    self.ui_stats['phase'] = 'complete'
                    self.ui_stats['current_file'] = ''
                    self.ui_stats['current_file_size'] = 0
                    self.ui_stats['current_file_received'] = 0
                self.log(f"Backup complete! {self.downloaded_count} files saved to Servers/{self.server_name}/")
        except Exception as e:
            error_msg = f"Backup error: {str(e)}"
            self.log(error_msg)
            import traceback
            tb = traceback.format_exc()
            print(tb)
            if self.debug_mode:
                try:
                    with open(self.debug_log_path, 'a', encoding='utf-8') as f:
                        f.write(f"[{time.strftime('%H:%M:%S')}] {error_msg}\n")
                        f.write(tb + "\n")
                except:
                    pass
            with self.ui_lock:
                self.ui_stats['status'] = f"Error: {str(e)}"
            self.log(f"Backup stopped with an error after {self.downloaded_count} files saved to Servers/{self.server_name}/")
        except KeyboardInterrupt:
            self.log("Backup interrupted by user")
            with self.ui_lock:
                self.ui_stats['status'] = 'Backup interrupted by user'
                self.ui_stats['phase'] = 'complete'
        finally:
            self.running = False
            if getattr(self, "grc_handle", None) and getattr(self, "grc", None):
                self.grc.rc_disconnect(self.grc_handle)
                self.grc_handle = None
            time.sleep(0.5)
            while self.ui_active:
                time.sleep(0.1)
    
    def _ui_loop(self):
        try:
            curses.wrapper(self._draw_progress_screen)
        except:
            pass
    
    def _draw_progress_screen(self, stdscr):
        stdscr.clear()
        stdscr.refresh()
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except:
            pass
        stdscr.nodelay(1)
        stdscr.keypad(True)
        
        server_select_index = 0
        
        try:
            curses.start_color()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_BLUE, -1)
            curses.init_pair(6, curses.COLOR_MAGENTA, -1)
            has_colors = True
        except:
            has_colors = False
        
        while self.ui_active:
            try:
                height, width = stdscr.getmaxyx()
                if height < 15 or width < 50:
                    stdscr.clear()
                    stdscr.addstr(0, 0, "Terminal too small. Please resize.")
                    stdscr.refresh()
                    time.sleep(0.5)
                    continue
                
                stdscr.clear()
                
                with self.ui_lock:
                    stats = self.ui_stats.copy()
                
                elapsed = time.time() - stats['start_time'] if stats['start_time'] > 0 else 0
                phase = stats.get('phase', 'init')
                
                header = "BackupBoi - Server Backup Tool"
                try:
                    stdscr.addstr(0, (width - len(header)) // 2, header, curses.A_BOLD if has_colors else 0)
                except:
                    pass
                
                try:
                    stdscr.addstr(1, 0, "=" * width)
                except:
                    pass
                
                y = 3
                
                if phase == 'select':
                    status = stats.get('status', 'Select server:')
                    try:
                        stdscr.addstr(y, 2, status[:width-4])
                    except:
                        pass
                    y += 2
                    
                    servers = stats.get('servers', [])
                    selected_idx = stats.get('selected_index', 0)
                    
                    max_servers = min(len(servers), height - y - 2, 20)
                    start_idx = max(0, min(selected_idx - max_servers // 2, len(servers) - max_servers))
                    
                    for i in range(start_idx, min(start_idx + max_servers, len(servers))):
                        server = servers[i]
                        is_selected = (i == selected_idx)
                        server_text = f"  {i+1}. {server['name']} ({server['players']} players)"
                        if len(server_text) > width - 4:
                            server_text = server_text[:width-7] + "..."
                        
                        try:
                            if is_selected:
                                stdscr.addstr(y, 2, server_text, curses.A_REVERSE if has_colors else curses.A_BOLD)
                            else:
                                stdscr.addstr(y, 2, server_text)
                        except:
                            pass
                        y += 1
                    
                    try:
                        key = stdscr.getch()
                        if key == curses.KEY_UP:
                            with self.ui_lock:
                                if self.ui_stats.get('selected_index', 0) > 0:
                                    self.ui_stats['selected_index'] -= 1
                        elif key == curses.KEY_DOWN:
                            with self.ui_lock:
                                if self.ui_stats.get('selected_index', 0) < len(servers) - 1:
                                    self.ui_stats['selected_index'] += 1
                        elif key == ord('\n') or key == ord('\r'):
                            with self.ui_lock:
                                self.ui_stats['server_selected'] = True
                    except:
                        pass
                else:
                    if phase != 'complete':
                        status = stats.get('status', 'Initializing...')
                        if len(status) > width - 10:
                            status = status[:width-13] + "..."
                        try:
                            stdscr.addstr(y, 2, f"* {status}")
                        except:
                            pass
                        y += 2
                    
                    if phase == 'complete':
                        try:
                            stdscr.addstr(height - 3, 2, "=" * (width - 4))
                        except:
                            pass
                        try:
                            complete_text = f"Backup Complete! Press any key to exit..."
                            stdscr.addstr(height - 2, (width - len(complete_text)) // 2, complete_text, curses.A_BOLD if has_colors else 0)
                        except:
                            pass
                        try:
                            key = stdscr.getch()
                            if key != -1:
                                self.ui_active = False
                                break
                        except:
                            pass
                    elif phase == 'backup':
                        total = stats.get('total_files', 0)
                        downloaded = stats.get('downloaded_count', 0)
                        bytes_dl = stats.get('bytes_downloaded', 0)
                        folders = stats.get('folders_scanned', 0)
                        download_start = stats.get('download_start_time', 0)
                        download_start_count = stats.get('download_start_count', 0)
                        download_start_bytes = stats.get('download_start_bytes', 0)
                        
                        if total == 0:
                            file_index_file = os.path.join(self.base_dir, "file_index.json")
                            if os.path.exists(file_index_file):
                                try:
                                    with open(file_index_file, 'r', encoding='utf-8') as f:
                                        file_index_data = json.load(f)
                                    total = len(file_index_data.get('files', []))
                                    with self.ui_lock:
                                        self.ui_stats['total_files'] = total
                                except:
                                    pass
                        
                        if total > 0:
                            percent = min((downloaded / total * 100), 100.0) if total > 0 else 0
                            progress_text = f"Progress: {percent:.1f}% ({downloaded}/{total} files)"
                            
                            if download_start > 0:
                                elapsed_dl = time.time() - download_start
                                files_done_this_run = max(0, downloaded - download_start_count)
                                bytes_done_this_run = max(0, bytes_dl - download_start_bytes)
                                remaining_files = max(0, total - downloaded)
                                remaining_bytes = max(0, stats.get('total_size', 0) - bytes_dl)
                                eta_seconds = None
                                if elapsed_dl >= 10 and bytes_done_this_run > 65536 and remaining_bytes > 0:
                                    byte_rate = bytes_done_this_run / elapsed_dl
                                    if byte_rate > 0:
                                        eta_seconds = remaining_bytes / byte_rate
                                elif elapsed_dl >= 30 and files_done_this_run >= 3 and remaining_files > 0:
                                    file_rate = files_done_this_run / elapsed_dl
                                    if file_rate > 0:
                                        eta_seconds = remaining_files / file_rate
                                if eta_seconds is not None:
                                    progress_text += f" | ETA: {self._format_time(eta_seconds)}"
                                elif remaining_files > 0:
                                    progress_text += " | ETA: calculating"
                        else:
                            progress_text = f"Progress: {downloaded} files downloaded"
                        
                        try:
                            stdscr.addstr(y, 2, progress_text[:width-4])
                        except:
                            pass
                        y += 1
                        
                        bar_width = min(width - 10, 60)
                        if total > 0:
                            percent_filled = min(downloaded / total, 1.0)
                            filled = int(bar_width * percent_filled)
                            if filled > bar_width:
                                filled = bar_width
                            if filled < 0:
                                filled = 0
                            progress_bar = '#' * filled + '-' * (bar_width - filled)
                        else:
                            anim_pos = int((time.time() * 2) % (bar_width * 2))
                            if anim_pos < bar_width:
                                progress_bar = '-' * anim_pos + '#' + '-' * (bar_width - anim_pos - 1)
                            else:
                                pos = bar_width * 2 - anim_pos
                                progress_bar = '-' * pos + '#' + '-' * (bar_width - pos - 1)
                        
                        try:
                            stdscr.addstr(y, 2, progress_bar[:width-10])
                        except:
                            pass
                        y += 2
                        
                        total_size_bytes = stats.get('total_size', 0)
                        if total_size_bytes > 0:
                            bytes_text = f"Size: {self._format_bytes(bytes_dl)} / {self._format_bytes(total_size_bytes)}"
                        else:
                            bytes_text = f"Downloaded: {self._format_bytes(bytes_dl)}"
                        try:
                            stdscr.addstr(y, 2, bytes_text[:width-4])
                        except:
                            pass
                        y += 1
                        
                        folders_text = f"Folders scanned: {folders}"
                        try:
                            stdscr.addstr(y, 2, folders_text[:width-4])
                        except:
                            pass
                        y += 1
                        
                        time_text = f"Elapsed: {self._format_time(elapsed)}"
                        try:
                            stdscr.addstr(y, 2, time_text[:width-4])
                        except:
                            pass
                        y += 2
                        
                        current_file = stats.get('current_file', '')
                        current_file_size = stats.get('current_file_size', 0)
                        current_file_received = stats.get('current_file_received', 0)
                        if current_file:
                            file_text = f"File: {current_file}"
                            if current_file_size > 0:
                                file_percent = min((current_file_received / current_file_size * 100), 100.0)
                                file_text += f" ({file_percent:.1f}%)"
                            if len(file_text) > width - 4:
                                file_text = file_text[:width-7] + "..."
                            try:
                                stdscr.addstr(y, 2, file_text)
                            except:
                                pass
                            y += 1
                        
                        current_folder = stats.get('current_folder', '')
                        if current_folder:
                            if len(current_folder) > width - 15:
                                current_folder = current_folder[:width-18] + "..."
                            try:
                                stdscr.addstr(y, 2, f"Folder: {current_folder}")
                            except:
                                pass
                            y += 1
                        
                        y += 1
                        try:
                            stdscr.addstr(y, 2, f"Server: {self.server_name}")
                        except:
                            pass
                        y += 1
                        
                        save_path = os.path.relpath(self.base_dir, os.path.dirname(__file__))
                        if len(save_path) > width - 15:
                            save_path = "..." + save_path[-(width-18):]
                        try:
                            stdscr.addstr(y, 2, f"Save: {save_path}")
                        except:
                            pass
                    else:
                        time_text = f"Elapsed: {self._format_time(elapsed)}"
                        try:
                            stdscr.addstr(y, 2, time_text[:width-4])
                        except:
                            pass
                
                stdscr.refresh()
                time.sleep(0.1)
            except:
                time.sleep(0.1)
    
    def _format_time(self, seconds):
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    
    def _format_bytes(self, bytes_val):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_val < 1024.0:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.2f} PB"


def _grc_decode(value):
    return value.decode('latin-1', errors='ignore') if value else ''


def _backupman_grclib_names():
    system = platform.system()
    if system == "Windows":
        return ["grclib.dll"]
    if system == "Darwin":
        return ["grclib.dylib"]
    return ["grclib.so"]


def _backupman_safe_relative_path(path):
    rel_path = str(path or "").replace('\\', '/').strip()
    if not rel_path:
        raise ValueError("empty remote file path")
    if re.match(r'^[A-Za-z]:', rel_path) or rel_path.startswith('/'):
        raise ValueError(f"absolute remote file path rejected: {rel_path}")
    normalized = os.path.normpath(rel_path).replace('\\', '/')
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"unsafe remote file path rejected: {rel_path}")
    return normalized


def _backupman_windows_safe_segment(segment):
    invalid_chars = set('<>:"|?*')
    reserved_names = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    out = []
    for ch in segment:
        code = ord(ch)
        if ch in invalid_chars or code < 32:
            out.append(f"%{code:02X}")
        else:
            out.append(ch)
    safe = ''.join(out)
    while safe.endswith(' ') or safe.endswith('.'):
        ch = safe[-1]
        safe = safe[:-1] + f"%{ord(ch):02X}"
    stem = safe.split('.', 1)[0].upper()
    if stem in reserved_names:
        safe = f"%{ord(safe[0]):02X}{safe[1:]}"
    return safe


def _backupman_local_path(base_dir, remote_path):
    safe_path = _backupman_safe_relative_path(remote_path)
    safe_path = '/'.join(_backupman_windows_safe_segment(part) for part in safe_path.split('/'))
    root = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(root, safe_path.replace('/', os.sep)))
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"remote file path escaped backup root: {remote_path}")
    return target


def _backupman_write_bytes_atomic(file_path, content):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp_path = f"{file_path}.part.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, 'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except:
            pass


def _backupman_write_json_atomic(file_path, data):
    encoded = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
    _backupman_write_bytes_atomic(file_path, encoded)


def _backupman_bind_grclib(self):
    configured = self.config.get("grclib_path")
    script_dir = os.path.dirname(__file__)
    candidates = [configured]
    candidates.extend(os.path.join(script_dir, name) for name in _backupman_grclib_names())
    dll_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if not dll_path:
        expected = ", ".join(_backupman_grclib_names())
        raise RuntimeError(f"GRCLib shared library not found beside _backupman.py ({expected}). Set grclib_path in config.json.")

    self.grc = ctypes.PyDLL(dll_path)
    g = self.grc
    g.rc_connect.argtypes = [c_char_p, c_int, c_char_p, c_char_p]
    g.rc_connect.restype = c_void_p
    g.rc_last_error.argtypes = [c_void_p]
    g.rc_last_error.restype = c_char_p
    g.rc_get_servers.argtypes = [c_void_p, POINTER(POINTER(RCServer))]
    g.rc_get_servers.restype = c_int
    g.rc_connect_to_server.argtypes = [c_void_p, c_int]
    g.rc_connect_to_server.restype = c_int
    if hasattr(g, "rc_set_login_pcid_list"):
        g.rc_set_login_pcid_list.argtypes = [c_void_p, c_char_p]
        g.rc_set_login_pcid_list.restype = None
    g.rc_disconnect.argtypes = [c_void_p]
    g.rc_disconnect.restype = None
    g.rc_process_events.argtypes = [c_void_p]
    g.rc_process_events.restype = None
    g.rc_on_connected.argtypes = [c_void_p, RC_OnConnected, c_void_p]
    g.rc_on_connected.restype = None
    g.rc_on_disconnected.argtypes = [c_void_p, RC_OnDisconnected, c_void_p]
    g.rc_on_disconnected.restype = None
    g.rc_on_message.argtypes = [c_void_p, RC_OnMessage, c_void_p]
    g.rc_on_message.restype = None
    g.rc_on_file_received.argtypes = [c_void_p, RC_OnFileReceived, c_void_p]
    g.rc_on_file_received.restype = None
    g.rc_on_filebrowser_folders.argtypes = [c_void_p, RC_OnFileBrowserFolders, c_void_p]
    g.rc_on_filebrowser_folders.restype = None
    g.rc_on_filebrowser_files.argtypes = [c_void_p, RC_OnFileBrowserFiles, c_void_p]
    g.rc_on_filebrowser_files.restype = None
    g.rc_on_filebrowser_message.argtypes = [c_void_p, RC_OnFileBrowserMessage, c_void_p]
    g.rc_on_filebrowser_message.restype = None
    g.rc_on_server_data.argtypes = [c_void_p, RC_OnServerData, c_void_p]
    g.rc_on_server_data.restype = None
    g.rc_get_filebrowser_folders.argtypes = [c_void_p, POINTER(POINTER(RCFileBrowserFolder))]
    g.rc_get_filebrowser_folders.restype = c_int
    g.rc_get_filebrowser_files.argtypes = [c_void_p, POINTER(POINTER(RCFileBrowserEntry))]
    g.rc_get_filebrowser_files.restype = c_int
    g.rc_copy_filebrowser_folders.argtypes = [c_void_p, POINTER(POINTER(RCFileBrowserFolder))]
    g.rc_copy_filebrowser_folders.restype = c_int
    g.rc_copy_filebrowser_files.argtypes = [c_void_p, POINTER(POINTER(RCFileBrowserEntry))]
    g.rc_copy_filebrowser_files.restype = c_int
    g.rc_free_filebrowser_folders.argtypes = [POINTER(RCFileBrowserFolder), c_int]
    g.rc_free_filebrowser_folders.restype = None
    g.rc_free_filebrowser_files.argtypes = [POINTER(RCFileBrowserEntry), c_int]
    g.rc_free_filebrowser_files.restype = None
    g.rc_filebrowser_start.argtypes = [c_void_p]
    g.rc_filebrowser_start.restype = c_int
    g.rc_filebrowser_cd.argtypes = [c_void_p, c_char_p]
    g.rc_filebrowser_cd.restype = c_int
    g.rc_filebrowser_download.argtypes = [c_void_p, c_char_p]
    g.rc_filebrowser_download.restype = c_int
    g.rc_request_server_options.argtypes = [c_void_p]
    g.rc_request_server_options.restype = c_int
    g.rc_request_server_flags.argtypes = [c_void_p]
    g.rc_request_server_flags.restype = c_int
    g.rc_request_folder_config.argtypes = [c_void_p]
    g.rc_request_folder_config.restype = c_int
    g.rc_get_server_options.argtypes = [c_void_p]
    g.rc_get_server_options.restype = c_void_p
    g.rc_get_server_flags.argtypes = [c_void_p]
    g.rc_get_server_flags.restype = c_void_p
    g.rc_get_folder_config.argtypes = [c_void_p]
    g.rc_get_folder_config.restype = c_void_p
    g.rc_free.argtypes = [c_void_p]
    g.rc_free.restype = None

    self.grc_handle = None
    self.grc_callbacks = {}
    self.grc_authenticated = threading.Event()
    self.grc_filebrowser_folders = threading.Event()
    self.grc_filebrowser_files = threading.Event()
    self.grc_download_events = {}
    self.grc_download_results = {}
    self.grc_lock = threading.RLock()
    self.log(f"Loaded GRCLib: {dll_path}", debug_only=True)


def _backupman_grc_event_loop(self):
    while self.running and getattr(self, "grc_handle", None):
        try:
            self.grc.rc_process_events(self.grc_handle)
        except Exception as e:
            self.log(f"GRCLib event error: {e}", debug_only=True)
        time.sleep(0.02)


def _backupman_setup_grc_callbacks(self):
    def on_connected(_user_data):
        try:
            self.disconnected = False
            self.disconnect_reason = ""
            self.authenticated = True
            self.grc_authenticated.set()
            self.log("Authenticated successfully!")
        except Exception as e:
            self.log(f"Connected callback error: {e}")

    def on_disconnected(reason, _user_data):
        try:
            self.disconnect_reason = _grc_decode(reason) or "Unknown"
            self.disconnected = True
            self.log("Disconnected: " + self.disconnect_reason)
            self.running = False
            self.grc_authenticated.set()
            self.pending_server_options.set()
            self.pending_server_flags.set()
            self.pending_folder_config.set()
            self.grc_filebrowser_folders.set()
            self.grc_filebrowser_files.set()
            with self.grc_lock:
                for event in list(self.grc_download_events.values()):
                    event.set()
        except Exception as e:
            self.log(f"Disconnected callback error: {e}")

    def on_message(message, _user_data):
        try:
            text = _grc_decode(message)
            if text:
                self.log(text, debug_only=True)
        except Exception as e:
            self.log(f"Message callback error: {e}")

    def on_filebrowser_message(message, _user_data):
        try:
            text = _grc_decode(message)
            if text:
                match = re.search(r"Received chunk:\s*(\d+)/(\d+)\s+bytes", text)
                if match:
                    with self.ui_lock:
                        self.ui_stats['current_file_received'] = int(match.group(1))
                        self.ui_stats['current_file_size'] = int(match.group(2))
                else:
                    failed_name = text.strip().replace('\\', '/')
                    with self.grc_lock:
                        pending = self.pending_file_download
                        event = self.grc_download_events.get(pending)
                        if pending and event:
                            pending_name = os.path.basename(pending.replace('\\', '/'))
                            if failed_name == pending or failed_name == pending_name:
                                self.grc_download_results[pending] = False
                                self.pending_file_download = None
                                event.set()
                                self.log(f"File download failed: {pending}", debug_only=True)
                self.log(text, debug_only=True)
        except Exception as e:
            self.log(f"File browser message callback error: {e}")

    def on_server_data(data_type, content, _user_data):
        try:
            data_type_text = _grc_decode(data_type)
            content_text = _grc_decode(content)
            if data_type_text == "options":
                self.server_options = content_text
                self.pending_server_options.set()
            elif data_type_text == "flags":
                self.server_flags = content_text
                self.pending_server_flags.set()
            elif data_type_text == "folder_config":
                self.folder_config = content_text
                self.pending_folder_config.set()
        except Exception as e:
            self.log(f"Server data callback error: {e}")

    def on_folders(count, _user_data):
        try:
            folders_ptr = POINTER(RCFileBrowserFolder)()
            real_count = self.grc.rc_copy_filebrowser_folders(self.grc_handle, ctypes.byref(folders_ptr))
            folders = []
            all_patterns = []
            try:
                for i in range(real_count):
                    item = folders_ptr[i]
                    rights = _grc_decode(item.rights)
                    pattern = _grc_decode(item.pattern)
                    if not pattern:
                        continue
                    row = {'name': pattern, 'rights': rights, 'pattern': pattern}
                    all_patterns.append(row)
                    if '*' not in pattern and '?' not in pattern:
                        folders.append(row)
            finally:
                self.grc.rc_free_filebrowser_folders(folders_ptr, real_count)
            with self.grc_lock:
                self.all_folder_patterns = all_patterns
                self.folders = folders
            self.log(f"Received {len(all_patterns)} total folder patterns, {len(folders)} without wildcards")
            self.grc_filebrowser_folders.set()
        except Exception as e:
            self.log(f"Folder callback error: {e}")
            self.grc_filebrowser_folders.set()

    def on_files(folder, count, _user_data):
        try:
            folder_text = _grc_decode(folder)
            entries_ptr = POINTER(RCFileBrowserEntry)()
            real_count = self.grc.rc_copy_filebrowser_files(self.grc_handle, ctypes.byref(entries_ptr))
            entries = []
            try:
                for i in range(real_count):
                    item = entries_ptr[i]
                    path = _grc_decode(item.path)
                    rights = _grc_decode(item.rights)
                    if not path:
                        continue
                    entries.append({
                        'path': path,
                        'rights': rights,
                        'size': int(item.size),
                        'modified': int(item.modified),
                        'is_directory': bool(item.is_directory) and not isZipPath(path),
                    })
            finally:
                self.grc.rc_free_filebrowser_files(entries_ptr, real_count)
            with self.grc_lock:
                self.current_folder = folder_text
                self.folder_files = entries
            self.log(f"File browser: {len(entries)} items", debug_only=True)
            self.grc_filebrowser_files.set()
            self.folder_contents_received.set()
        except Exception as e:
            self.log(f"Files callback error: {e}")
            self.grc_filebrowser_files.set()
            self.folder_contents_received.set()

    def on_file_received(path, content, length, _user_data):
        try:
            path_text = _grc_decode(path) or self.pending_file_download or "downloaded_file"
            save_path = path_text
            with self.grc_lock:
                if save_path not in self.grc_download_events:
                    basename = os.path.basename(save_path.replace('\\', '/'))
                    for pending_path in list(self.grc_download_events.keys()):
                        if os.path.basename(pending_path.replace('\\', '/')) == basename:
                            save_path = pending_path
                            break
            content_bytes = ctypes.string_at(content, length) if content and length > 0 else b''
            local_file_path = _backupman_local_path(self.base_dir, save_path)
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
            _backupman_write_bytes_atomic(local_file_path, content_bytes)
            with self.ui_lock:
                self.ui_stats['bytes_downloaded'] += len(content_bytes)
                self.ui_stats['current_file_size'] = 0
                self.ui_stats['current_file_received'] = 0
            with self.grc_lock:
                self.grc_download_results[save_path] = True
                event = self.grc_download_events.get(save_path)
            if self.pending_file_download == save_path:
                self.pending_file_download = None
            if event:
                event.set()
            self.log(f"Saved file: {save_path}", debug_only=True)
        except Exception as e:
            self.log(f"File receive callback error: {e}")
            with self.grc_lock:
                event = self.grc_download_events.get(self.pending_file_download)
            if event:
                event.set()

    self.grc_callbacks = {
        "connected": RC_OnConnected(on_connected),
        "disconnected": RC_OnDisconnected(on_disconnected),
        "message": RC_OnMessage(on_message),
        "file_received": RC_OnFileReceived(on_file_received),
        "folders": RC_OnFileBrowserFolders(on_folders),
        "files": RC_OnFileBrowserFiles(on_files),
        "fb_message": RC_OnFileBrowserMessage(on_filebrowser_message),
        "server_data": RC_OnServerData(on_server_data),
    }
    g = self.grc
    g.rc_on_connected(self.grc_handle, self.grc_callbacks["connected"], None)
    g.rc_on_disconnected(self.grc_handle, self.grc_callbacks["disconnected"], None)
    g.rc_on_message(self.grc_handle, self.grc_callbacks["message"], None)
    g.rc_on_file_received(self.grc_handle, self.grc_callbacks["file_received"], None)
    g.rc_on_filebrowser_folders(self.grc_handle, self.grc_callbacks["folders"], None)
    g.rc_on_filebrowser_files(self.grc_handle, self.grc_callbacks["files"], None)
    g.rc_on_filebrowser_message(self.grc_handle, self.grc_callbacks["fb_message"], None)
    g.rc_on_server_data(self.grc_handle, self.grc_callbacks["server_data"], None)


def _backupman_fetch_server_list(self, listserver_config):
    if not hasattr(self, "grc"):
        _backupman_bind_grclib(self)
    host = listserver_config['host']
    port = int(listserver_config['port'])
    account = listserver_config['account']
    password = listserver_config['password']
    self.log(f"Connecting to listserver {host}:{port}")
    self.grc_handle = self.grc.rc_connect(host.encode('latin-1'), port, account.encode('latin-1'), password.encode('latin-1'))
    if not self.grc_handle:
        raise RuntimeError("GRCLib rc_connect returned null")
    error = self.grc.rc_last_error(self.grc_handle)
    if error:
        self.log("Listserver warning: " + _grc_decode(error), debug_only=True)
    servers_ptr = POINTER(RCServer)()
    count = self.grc.rc_get_servers(self.grc_handle, ctypes.byref(servers_ptr))
    servers = []
    for i in range(count):
        item = servers_ptr[i]
        raw_name = _grc_decode(item.name)
        name = raw_name[2:] if len(raw_name) > 2 and raw_name[1] == ' ' else raw_name
        servers.append({
            "name": name,
            "raw_name": raw_name,
            "ip": _grc_decode(item.ip),
            "port": int(item.port),
            "players": int(item.players),
            "type": "grclib",
            "grc_index": i,
        })
    self.servers = servers
    return servers


def _backupman_connect_game_server(self, server):
    self.selected_server = server
    self.log(f"Connecting to {server['name']} at {server.get('ip', '')}:{server.get('port', '')}")
    _backupman_setup_grc_callbacks(self)
    if self.config.get("backupboi_compat_pcid", True) and hasattr(self.grc, "rc_set_login_pcid_list"):
        pcid_list = generateBackupPcidList(self.get_listserver_config().get("account", ""))
        self.grc.rc_set_login_pcid_list(self.grc_handle, pcid_list.encode('latin-1'))
    self.running = True
    if not getattr(self, "receive_thread", None) or not self.receive_thread.is_alive():
        self.receive_thread = threading.Thread(target=_backupman_grc_event_loop, args=(self,), daemon=True)
        self.receive_thread.start()
    self.grc_authenticated.clear()
    self.disconnected = False
    self.disconnect_reason = ""
    result = self.grc.rc_connect_to_server(self.grc_handle, int(server["grc_index"]))
    if result != 1:
        error = self.grc.rc_last_error(self.grc_handle)
        self.log("Connection failed: " + (_grc_decode(error) or "Unknown error"))
        return False
    if not self.grc_authenticated.wait(timeout=20):
        self.log("Authentication timeout")
        return False
    if self.disconnected:
        return False
    return True


def _backupman_open_file_browser(self):
    self.log("Opening file browser...")
    self.grc_filebrowser_folders.clear()
    self.grc_filebrowser_files.clear()
    self.folder_contents_received.clear()
    if self.grc.rc_filebrowser_start(self.grc_handle) != 1:
        return False
    self.grc_filebrowser_folders.wait(timeout=15)
    self.grc_filebrowser_files.wait(timeout=5)
    return True


def _backupman_change_folder(self, folder_path):
    if folder_path and not folder_path.endswith('/'):
        folder_path += '/'
    self.log(f"Changing to folder: {folder_path}")
    self.folder_files = []
    self.expecting_folder_contents = True
    self.folder_contents_received.clear()
    self.grc_filebrowser_files.clear()
    if self.grc.rc_filebrowser_cd(self.grc_handle, folder_path.encode('latin-1')) != 1:
        return False
    if self.grc_filebrowser_files.wait(timeout=10):
        return True
    self.log(f"  Timeout waiting for folder contents: {folder_path}")
    return False


def _backupman_download_folder_for_path(file_path):
    folder_path = os.path.dirname(file_path).replace('\\', '/')
    return "" if not folder_path else folder_path + "/"


def _backupman_refresh_filebrowser_for_download(self, file_path):
    folder_path = _backupman_download_folder_for_path(file_path)
    self.log(f"  Refreshing file browser after timeout: {folder_path or 'root'}")
    with self.ui_lock:
        self.ui_stats['status'] = f"Refreshing file browser: {os.path.basename(file_path)}"
    if not _backupman_open_file_browser(self):
        self.log("  Failed to reopen file browser")
        return False
    if not _backupman_change_folder(self, folder_path):
        self.log(f"  Failed to refresh folder after timeout: {folder_path or 'root'}")
        return False
    return True


def _backupman_download_matches_expected(self, file_path, expected_size):
    local_file_path = _backupman_local_path(self.base_dir, file_path)
    if not os.path.exists(local_file_path):
        self.log(f"  Download completed but file not found: {file_path}")
        return False
    actual_size = os.path.getsize(local_file_path)
    if expected_size > 0 and actual_size != expected_size:
        self.log(f"  Download size mismatch: {file_path} (expected={expected_size}, actual={actual_size})")
        return False
    return True


def _backupman_download_file_once(self, file_path, timeout_seconds, request_path=None, expected_size=0):
    request_path = request_path or file_path
    self.update_progress(file_path, "")
    self.pending_file_download = file_path
    event = threading.Event()
    with self.grc_lock:
        self.grc_download_events[file_path] = event
        self.grc_download_results[file_path] = False
    with self.ui_lock:
        self.ui_stats['current_file'] = os.path.basename(file_path)
        self.ui_stats['current_file_received'] = 0
    self.log(f"  Requesting download: {request_path} -> {file_path}", debug_only=True)
    if self.grc.rc_filebrowser_download(self.grc_handle, request_path.encode('latin-1')) != 1:
        self.pending_file_download = None
        with self.grc_lock:
            self.grc_download_events.pop(file_path, None)
            self.grc_download_results.pop(file_path, None)
        return False
    timeout = time.time() + timeout_seconds
    while time.time() < timeout:
        if event.wait(timeout=0.2):
            ok = bool(self.grc_download_results.get(file_path))
            with self.grc_lock:
                self.grc_download_events.pop(file_path, None)
                self.grc_download_results.pop(file_path, None)
            return ok and _backupman_download_matches_expected(self, file_path, expected_size)
        if self.pending_file_download is None:
            ok = _backupman_download_matches_expected(self, file_path, expected_size)
            with self.grc_lock:
                self.grc_download_events.pop(file_path, None)
                self.grc_download_results.pop(file_path, None)
            return ok
    self.log(f"  Timeout downloading: {file_path}")
    self.pending_file_download = None
    with self.grc_lock:
        self.grc_download_events.pop(file_path, None)
        self.grc_download_results.pop(file_path, None)
    return False


def _backupman_download_file(self, file_path):
    expected_size = int(self.ui_stats.get('current_file_size') or 0)
    timeout_seconds = max(20, min(300, int(expected_size / 32768) + 20))
    basename = os.path.basename(file_path.replace('\\', '/'))
    first_request = basename or file_path
    if _backupman_download_file_once(self, file_path, timeout_seconds, request_path=first_request, expected_size=expected_size):
        return True
    if _backupman_refresh_filebrowser_for_download(self, file_path):
        self.log(f"  Retrying after file browser refresh: {file_path}")
        if _backupman_download_file_once(self, file_path, timeout_seconds, request_path=first_request, expected_size=expected_size):
            return True
        if basename and basename != file_path:
            self.log(f"  Retrying with full path after basename failed: {file_path}")
            if _backupman_download_file_once(self, file_path, timeout_seconds, request_path=file_path, expected_size=expected_size):
                return True
    return False


def _backupman_request_server_options(self):
    self.log("Requesting server options...")
    self.pending_server_options.clear()
    self.server_options = None
    if self.grc.rc_request_server_options(self.grc_handle) != 1:
        self.log("Failed to request server options through GRCLib")
        return False
    if self.pending_server_options.wait(timeout=10):
        if self.disconnected:
            return False
        return True
    value_ptr = self.grc.rc_get_server_options(self.grc_handle)
    if value_ptr:
        try:
            self.server_options = _grc_decode(ctypes.cast(value_ptr, c_char_p).value)
            return True
        finally:
            self.grc.rc_free(value_ptr)
    self.log("Timeout waiting for server options")
    return False


def _backupman_request_server_flags(self):
    self.log("Requesting server flags...")
    self.pending_server_flags.clear()
    self.server_flags = None
    if self.grc.rc_request_server_flags(self.grc_handle) != 1:
        self.log("Failed to request server flags through GRCLib")
        return False
    if self.pending_server_flags.wait(timeout=10):
        if self.disconnected:
            return False
        return True
    value_ptr = self.grc.rc_get_server_flags(self.grc_handle)
    if value_ptr:
        try:
            self.server_flags = _grc_decode(ctypes.cast(value_ptr, c_char_p).value)
            return True
        finally:
            self.grc.rc_free(value_ptr)
    self.log("Timeout waiting for server flags")
    return False


def _backupman_request_folder_config(self):
    self.log("Requesting folder config...")
    self.pending_folder_config.clear()
    self.folder_config = None
    if self.grc.rc_request_folder_config(self.grc_handle) != 1:
        self.log("Failed to request folder config through GRCLib")
        return False
    if self.pending_folder_config.wait(timeout=10):
        if self.disconnected:
            return False
        return True
    value_ptr = self.grc.rc_get_folder_config(self.grc_handle)
    if value_ptr:
        try:
            self.folder_config = _grc_decode(ctypes.cast(value_ptr, c_char_p).value)
            return True
        finally:
            self.grc.rc_free(value_ptr)
    self.log("Timeout waiting for folder config")
    return False


BackupBoi.fetch_server_list = _backupman_fetch_server_list
BackupBoi.connect_game_server = _backupman_connect_game_server
BackupBoi.open_file_browser = _backupman_open_file_browser
BackupBoi.change_folder = _backupman_change_folder
BackupBoi.download_file = _backupman_download_file
BackupBoi.request_server_options = _backupman_request_server_options
BackupBoi.request_server_flags = _backupman_request_server_flags
BackupBoi.request_folder_config = _backupman_request_folder_config

if __name__ == "__main__":
    boi = BackupBoi()
    boi.run()
