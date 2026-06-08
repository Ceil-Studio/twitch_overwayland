#!/usr/bin/env python3

import gi
import threading
import socket
import re
import sys
import hashlib
import urllib.request
import subprocess

gi.require_version('Gtk', '3.0')
gi.require_version('GtkLayerShell', '0.1')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GtkLayerShell, GLib, Pango, GdkPixbuf
import cairo

CHANNEL = "ceilciuz"
MESSAGE_TIMEOUT = 15  
AUDIO_FILE = "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga" 

class EmoteManager:
    def __init__(self):
        self.cache = {}
        self.target_size = 32 # Taille des emotes proportionnelle au nouveau texte

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

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, 20)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, 40)
        
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        empty_region = cairo.Region()
        self.input_shape_combine_region(empty_region)

        self.set_size_request(350, -1)
        
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        
        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.scrolled_window.add(self.vbox)
        self.add(self.scrolled_window)
        
        # Le CSS ne gère plus la police, uniquement les fonds et l'ombre
        css_provider = Gtk.CssProvider()
        css = """
        window, scrolledwindow, viewport, box { 
            background-color: transparent; 
            background-image: none;
        }
        textview {
            background-color: rgba(0, 0, 0, 0.65);
            border-radius: 8px;
        }
        textview text {
            background-color: transparent;
            text-shadow: 1px 1px 2px black, -1px -1px 2px black;
        }
        """
        css_provider.load_from_data(css.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_screen(
            screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.irc_thread = threading.Thread(target=self.twitch_irc_worker, daemon=True)
        self.irc_thread.start()

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
        try:
            subprocess.Popen(
                ['pw-play', '--volume=2.5', AUDIO_FILE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

    def remove_message_widget(self, widget):
        if widget in self.vbox.get_children():
            self.vbox.remove(widget)
            widget.destroy()
        return False

    def _scroll_to_bottom(self):
        adj = self.scrolled_window.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def append_message(self, user, message, emotes_raw=""):
        msg_view = Gtk.TextView()
        msg_view.set_editable(False)
        msg_view.set_cursor_visible(False)
        msg_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        
        buf = msg_view.get_buffer()

        # Forçage absolu de la typographie via Pango (court-circuite le CSS)
        font_desc = "sans-serif bold 16"
        base_tag = buf.create_tag("base_text", font=font_desc, foreground="white")
        
        color = self.get_user_color(user)
        user_tag = buf.create_tag(user, font=font_desc, foreground=color)

        emotes_list = []
        if emotes_raw:
            for emote_data in emotes_raw.split('/'):
                if not emote_data: continue
                e_id, positions = emote_data.split(':')
                for pos in positions.split(','):
                    start, end = map(int, pos.split('-'))
                    emotes_list.append((start, end, e_id))
        
        emotes_list.sort(key=lambda x: x[0])

        # Insertion du pseudo
        buf.insert_with_tags(buf.get_end_iter(), user, user_tag)
        buf.insert_with_tags(buf.get_end_iter(), ": ", base_tag)

        # Insertion du message
        current_idx = 0
        for start, end, e_id in emotes_list:
            if current_idx < start:
                text_part = message[current_idx:start]
                buf.insert_with_tags(buf.get_end_iter(), text_part, base_tag)
            
            pixbuf = self.emote_manager.get_emote_pixbuf(e_id)
            if pixbuf:
                buf.insert_pixbuf(buf.get_end_iter(), pixbuf)
            else:
                emote_name = message[start:end+1]
                buf.insert_with_tags(buf.get_end_iter(), emote_name, base_tag)

            current_idx = end + 1

        if current_idx < len(message):
            buf.insert_with_tags(buf.get_end_iter(), message[current_idx:], base_tag)

        self.vbox.pack_start(msg_view, False, False, 0)
        msg_view.show_all()

        if user != "SYSTEM":
            self.play_notification_sound()

        children = self.vbox.get_children()
        if len(children) > 50:
            self.vbox.remove(children[0])
            children[0].destroy()

        GLib.timeout_add(MESSAGE_TIMEOUT * 1000, self.remove_message_widget, msg_view)
        GLib.idle_add(self._scroll_to_bottom)

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
