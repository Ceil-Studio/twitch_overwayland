#!/usr/bin/env python3

import gi
import threading
import socket
import re
import sys
import hashlib
import urllib.request
import time
import subprocess

gi.require_version('Gtk', '3.0')
gi.require_version('GtkLayerShell', '0.1')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GtkLayerShell, GLib, Pango, GdkPixbuf
import cairo

CHANNEL = "ceilciuz"
MESSAGE_TIMEOUT = 15  # Temps en secondes avant la disparition du message
AUDIO_FILE = "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga" # Chemin du pop audio

class EmoteManager:
    """Gère le téléchargement synchrone (dans le thread IRC) et la mise en cache des emotes."""
    def __init__(self):
        self.cache = {}
        self.target_size = 24 

    def get_emote_pixbuf(self, emote_id):
        if emote_id in self.cache:
            return self.cache[emote_id]

        url = f"https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/dark/1.0"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                image_data = response.read()
                
            loader = GdkPixbuf.PixbufLoader()
            loader.write(image_data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            
            scaled_pixbuf = pixbuf.scale_simple(
                self.target_size, self.target_size, GdkPixbuf.InterpType.BILINEAR
            )
            
            self.cache[emote_id] = scaled_pixbuf
            return scaled_pixbuf
        except Exception as e:
            print(f"Erreur téléchargement emote {emote_id}: {e}")
            return None


class TwitchOverlay(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.emote_manager = EmoteManager()
        self.message_queue = []

        # 1. Configuration Wayland Layer Shell
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, 20)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 40)
        
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        # 2. Configuration Transparence et Click-Through
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        empty_region = cairo.Region()
        self.input_shape_combine_region(empty_region)

        self.set_size_request(350, -1)
        
        # 3. Interface GTK
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        
       # CSS : Renforcement extrême de l'ombre pour lisibilité sur fond blanc
        css_provider = Gtk.CssProvider()
        css = b"""
        window, scrolledwindow, textview, textview text { 
            background-color: transparent; 
            background-image: none;
        }
        textview { 
            color: white; 
            font-family: sans-serif; 
            font-size: 15px; 
            font-weight: 900;
            text-shadow: 
                0px 0px 4px rgba(0,0,0,1),
                0px 0px 8px rgba(0,0,0,1),
                2px 2px 2px rgba(0,0,0,1),
                -2px -2px 2px rgba(0,0,0,1),
                2px -2px 2px rgba(0,0,0,1),
                -2px 2px 2px rgba(0,0,0,1);
        }
        """
        css_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.textbuffer = self.textview.get_buffer()
        self.scrolled_window.add(self.textview)
        self.add(self.scrolled_window)

        # 4. Lancement des workers
        self.irc_thread = threading.Thread(target=self.twitch_irc_worker, daemon=True)
        self.irc_thread.start()

        GLib.timeout_add_seconds(1, self.cleanup_old_messages)

    def get_user_color(self, username):
        if username == "SYSTEM": return "#ff0000"
        hash_val = int(hashlib.md5(username.encode()).hexdigest(), 16)
        r = (hash_val & 0xFF0000) >> 16
        g = (hash_val & 0x00FF00) >> 8
        b = hash_val & 0x0000FF
        max_val = max(r, g, b)
        if max_val == 0: return "#ffffff"
        r = int((r / max_val) * 255); g = int((g / max_val) * 255); b = int((b / max_val) * 255)
        return f"#{r:02x}{g:02x}{b:02x}"

    def play_notification_sound(self):
        """Joue le son de manière asynchrone via PipeWire/PulseAudio"""
        try:
            subprocess.Popen(
                ['pw-play', '--volume=5', AUDIO_FILE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass # Silencieux si paplay échoue ou fichier manquant

    def append_message(self, user, message, emotes_raw=""):
        # Initialisation du marqueur de début
        start_iter = self.textbuffer.get_end_iter()
        start_mark = self.textbuffer.create_mark(None, start_iter, left_gravity=True)

        emotes_list = []
        if emotes_raw:
            for emote_data in emotes_raw.split('/'):
                if not emote_data: continue
                e_id, positions = emote_data.split(':')
                for pos in positions.split(','):
                    start, end = map(int, pos.split('-'))
                    emotes_list.append((start, end, e_id))
        
        emotes_list.sort(key=lambda x: x[0])

        color = self.get_user_color(user)
        tag_table = self.textbuffer.get_tag_table()
        tag = tag_table.lookup(user)
        if not tag:
            tag = self.textbuffer.create_tag(user, foreground=color, weight=Pango.Weight.BOLD)

        end_iter = self.textbuffer.get_end_iter()
        self.textbuffer.insert_with_tags(end_iter, user, tag)
        self.textbuffer.insert(self.textbuffer.get_end_iter(), ": ")

        current_idx = 0
        for start, end, e_id in emotes_list:
            if current_idx < start:
                text_part = message[current_idx:start]
                self.textbuffer.insert(self.textbuffer.get_end_iter(), text_part)
            
            pixbuf = self.emote_manager.get_emote_pixbuf(e_id)
            if pixbuf:
                self.textbuffer.insert_pixbuf(self.textbuffer.get_end_iter(), pixbuf)
            else:
                emote_name = message[start:end+1]
                self.textbuffer.insert(self.textbuffer.get_end_iter(), emote_name)

            current_idx = end + 1

        if current_idx < len(message):
            self.textbuffer.insert(self.textbuffer.get_end_iter(), message[current_idx:])
            
        self.textbuffer.insert(self.textbuffer.get_end_iter(), "\n")

        # Déclenchement du son uniquement si ce n'est pas le système
        if user != "SYSTEM":
            self.play_notification_sound()

        # Initialisation du marqueur de fin et ajout à la file
        end_iter = self.textbuffer.get_end_iter()
        end_mark = self.textbuffer.create_mark(None, end_iter, left_gravity=False)
        self.message_queue.append((time.time(), start_mark, end_mark))

        adj = self.scrolled_window.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def cleanup_old_messages(self):
        current_time = time.time()
        messages_to_remove = []

        for item in self.message_queue:
            timestamp, start_mark, end_mark = item
            if current_time - timestamp > MESSAGE_TIMEOUT:
                messages_to_remove.append(item)
            else:
                break 

        if messages_to_remove:
            self.textbuffer.begin_user_action()
            for item in messages_to_remove:
                timestamp, start_mark, end_mark = item
                
                start_iter = self.textbuffer.get_iter_at_mark(start_mark)
                end_iter = self.textbuffer.get_iter_at_mark(end_mark)
                
                self.textbuffer.delete(start_iter, end_iter)
                
                self.textbuffer.delete_mark(start_mark)
                self.textbuffer.delete_mark(end_mark)
                
                self.message_queue.remove(item)
            self.textbuffer.end_user_action()

        return True 

    def _safe_append(self, user, message, emotes_raw=""):
        GLib.idle_add(self.append_message, user, message, emotes_raw)

    def twitch_irc_worker(self):
        server = 'irc.chat.twitch.tv'
        port = 6667
        nickname = 'justinfan12345'
        
        sock = socket.socket()
        try:
            sock.connect((server, port))
            sock.send(f"NICK {nickname}\n".encode('utf-8'))
            sock.send("CAP REQ :twitch.tv/tags twitch.tv/commands\n".encode('utf-8'))
            sock.send(f"JOIN #{CHANNEL}\n".encode('utf-8'))
            
            self._safe_append("SYSTEM", f"Connecté à #{CHANNEL}")

            while True:
                resp = sock.recv(4096).decode('utf-8', errors='ignore')
                if not resp: break

                lines = resp.split('\r\n')
                for line in lines:
                    if not line: continue
                    
                    if line.startswith('PING'):
                        sock.send("PONG\n".encode('utf-8'))
                        continue

                    match = re.search(r'^(?:@([^ ]+) )?:(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)$', line)
                    if match:
                        tags_str = match.group(1) or ""
                        user = match.group(2)
                        msg = match.group(3)
                        
                        emotes_raw = ""
                        for tag in tags_str.split(';'):
                            if tag.startswith('emotes='):
                                emotes_raw = tag[7:]
                                break
                                
                        self._safe_append(user, msg, emotes_raw)

        except Exception as e:
            self._safe_append("SYSTEM", f"Erreur IRC: {str(e)}")

if __name__ == '__main__':
    app = TwitchOverlay()
    app.show_all()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        sys.exit(0)
