import os
import hashlib
import configparser
import threading
import queue
from tkinter import Tk, Label, Button, Frame, scrolledtext, filedialog, Toplevel, Text
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError

CONFIG_FILE = 'modsync.ini'


def calculate_local_md5(file_path, buffer_size=8192):
    md5 = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            while True:
                data = f.read(buffer_size)
                if not data:
                    break
                md5.update(data)
    except IOError:
        return None
    return md5.hexdigest()

def get_s3_file_inventory(s3_client, bucket_name, log_queue):
    inventory = {}
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket_name):
            if "Contents" in page:
                for obj in page['Contents']:
                    # ETag 通常带有双引号，需要移除
                    inventory[obj['Key']] = obj['ETag'].strip('"')
    except ClientError as e:
        log_queue.put(f"错误: 无法访问S3存储桶 '{bucket_name}'. 请检查存储桶名称和权限。")
        log_queue.put(f"S3 错误信息: {e}")
        return None
    return inventory

def get_local_file_inventory(directory, log_queue):
    inventory = {}
    if not os.path.isdir(directory):
        log_queue.put(f"错误: 本地目录不存在: {directory}")
        return {}
        
    for root, _, files in os.walk(directory):
        for filename in files:
            file_path = os.path.join(root, filename)
            relative_path = os.path.relpath(file_path, directory).replace('\\', '/')
            inventory[relative_path] = calculate_local_md5(file_path)
    return inventory

class S3SyncApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Chouchou服务器模组同步APP")
        self.root.geometry("700x500")

        self.config = configparser.ConfigParser()
        if not os.path.exists(CONFIG_FILE):
            self.show_error_popup("配置文件丢失", f"错误: 未找到 {CONFIG_FILE} 文件。\n\n请在程序目录下创建该文件。")
            root.destroy()
            return
            
        self.config.read(CONFIG_FILE)
        self.sync_dir = self.config.get('Local', 'sync_directory', fallback='.')
        self.s3_bucket = self.config.get('S3', 'bucket_name', fallback='N/A')

        top_frame = Frame(root, padx=10, pady=5)
        top_frame.pack(fill='x', side='top')

        Label(top_frame, text="同步目录（请检查是否为模组文件夹）:").pack(side='left')
        self.dir_label = Label(top_frame, text=os.path.abspath(self.sync_dir), fg="blue", anchor='w')
        self.dir_label.pack(side='left', fill='x', expand=True)

        controls_frame = Frame(root, padx=10, pady=10)
        controls_frame.pack(fill='x', side='top')

        self.update_button = Button(controls_frame, text="开始同步", command=self.start_sync_thread, font=("Arial", 12, "bold"), bg="#4CAF50", fg="white")
        self.update_button.pack(side='left', expand=True, fill='x', ipady=5)

        self.change_dir_button = Button(controls_frame, text="更改目录", command=self.change_directory)
        self.change_dir_button.pack(side='left', padx=(10, 0))

        log_frame = Frame(root, padx=10, pady=5)
        log_frame.pack(fill='both', expand=True)
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap='word', state='disabled')
        self.log_area.pack(fill='both', expand=True)

        self.log_queue = queue.Queue()
        self.root.after(100, self.process_log_queue)

        self.log_message(f"模组同步工具已就绪。")
        self.log_message(f"将与存储桶 '{self.s3_bucket}' 同步。")
        self.log_message(f"请注意储存桶位置")
        self.log_message(f"本地目录: {os.path.abspath(self.sync_dir)}")
        self.log_message("请注意本地目录")
        self.log_message("点击 '开始同步' 按钮启动。")

    def show_error_popup(self, title, message):
        error_win = Toplevel(self.root)
        error_win.title(title)
        Label(error_win, text=message, padx=20, pady=20).pack()
        Button(error_win, text="确定", command=error_win.destroy).pack(pady=10)

    def log_message(self, message):
        self.log_area.config(state='normal')
        self.log_area.insert('end', message + '\n')
        self.log_area.config(state='disabled')
        self.log_area.see('end')

    def process_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_message(message)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_log_queue)
    
    def change_directory(self):
        new_dir = filedialog.askdirectory(title="选择新的同步目录", initialdir=self.sync_dir)
        if new_dir:
            self.sync_dir = new_dir
            self.dir_label.config(text=os.path.abspath(self.sync_dir))
            
            self.config.set('Local', 'sync_directory', self.sync_dir)
            try:
                with open(CONFIG_FILE, 'w') as configfile:
                    self.config.write(configfile)
                self.log_message(f"同步目录已更改为: {os.path.abspath(self.sync_dir)}")
            except IOError:
                self.log_message("错误: 无法写入配置文件（你是给删了还是咋地，删了程序跑不起来的哦）")

    def set_ui_state(self, enabled):
        state = 'normal' if enabled else 'disabled'
        self.update_button.config(state=state)
        self.change_dir_button.config(state=state)

    def start_sync_thread(self):
        self.set_ui_state(False)
        self.log_area.config(state='normal')
        self.log_area.delete('1.0', 'end')
        self.log_area.config(state='disabled')#UI冻结你妈死了
        
        sync_thread = threading.Thread(target=self.run_sync, daemon=True)
        sync_thread.start()

    def run_sync(self):
        """同步操作的实际执行函数"""
        try:
            self.log_queue.put("--- 开始同步 ---")
            self.log_queue.put("正在读取配置文件...")
            
            s3_config = self.config['S3']
            s3_client = boto3.client(
                's3',
                aws_access_key_id=s3_config['aws_access_key_id'],
                aws_secret_access_key=s3_config['aws_secret_access_key'],
                endpoint_url=s3_config.get('endpoint_url', None)
            )

            bucket_name = s3_config['bucket_name']
            
            self.log_queue.put("正在从服务器获取模组列表...")
            s3_inventory = get_s3_file_inventory(s3_client, bucket_name, self.log_queue)
            if s3_inventory is None:
                raise Exception("无法继续，获取服务器文件列表失败。")

            self.log_queue.put(f"服务端发现 {len(s3_inventory)} 个文件")

            self.log_queue.put("正在扫描本地文件...")
            local_directory = os.path.abspath(self.sync_dir)
            if not os.path.exists(local_directory):
                self.log_queue.put(f"本地目录 '{local_directory}' 不存在")
                raise Exception("无法继续，本地目录不存在")
            local_inventory = get_local_file_inventory(local_directory, self.log_queue)
            self.log_queue.put(f"本地发现 {len(local_inventory)} 个文件")
            
            files_to_download = []
            files_to_delete = []
            self.log_queue.put("Let me check it")

            for s3_key, s3_etag in s3_inventory.items():
                if s3_key not in local_inventory or local_inventory[s3_key] != s3_etag:
                    files_to_download.append(s3_key)

            for local_path, _ in local_inventory.items():
                if local_path not in s3_inventory:
                    files_to_delete.append(local_path)
            
            self.log_queue.put("--- 对比完成 ---")
            self.log_queue.put(f"需要下载/更新 {len(files_to_download)} 个文件。")
            self.log_queue.put(f"需要删除 {len(files_to_delete)} 个文件。")

            if files_to_download:
                self.log_queue.put("\n--- 开始下载 ---")
                for key in files_to_download:
                    local_path = os.path.join(local_directory, key)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    try:
                        self.log_queue.put(f"下载中: {key}")
                        s3_client.download_file(bucket_name, key, local_path)
                    except Exception as e:
                        self.log_queue.put(f"!! 下载失败: {key} - {e}")
            
            if files_to_delete:
                self.log_queue.put("\n--- 开始删除本地多余文件 ---")
                for path in files_to_delete:
                    local_path = os.path.join(local_directory, path)
                    try:
                        self.log_queue.put(f"删除中: {path}")
                        os.remove(local_path)
                    except Exception as e:
                        self.log_queue.put(f"!! 删除失败: {path} - {e}")

            self.log_queue.put("\n--- 同步完成！ ---")

        except (NoCredentialsError, PartialCredentialsError):
            self.log_queue.put("错误: AWS凭证未找到或不完整。请检查服务器配置 'modsync.ini' 文件。")
        except KeyError as e:
            self.log_queue.put(f"错误: 配置文件 'modsync.ini' 中缺少键: {e}")
        except Exception as e:
            self.log_queue.put(f"发生未知错误，我不知道？: {e}")
        finally:
            self.root.after(0, self.set_ui_state, True)

if __name__ == "__main__":
    root = Tk()
    app = S3SyncApp(root)
    root.mainloop()