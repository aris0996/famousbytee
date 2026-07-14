#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
"""
╔══════════════════════════════════════════════════════╗
║         SERVER SECURITY ANALYZER  v1.0               ║
║         by famousbytee — CLI Audit Tool              ║
╚══════════════════════════════════════════════════════╝

Usage:
  python server_analyzer.py --host <IP> --port <PORT> --user <USER> --password <PASS>
  python server_analyzer.py --host 103.210.121.29 --port 1990 --user faaris --password 090906sept
  python server_analyzer.py --host 103.210.121.29 --port 1990 --user faaris --password 090906sept --module attacks
  python server_analyzer.py --host 103.210.121.29 --port 1990 --user faaris --password 090906sept --block-ip 62.60.130.237
"""

import argparse
import sys
import time
import socket
import re
import json
import datetime
from collections import Counter, defaultdict

try:
    import paramiko
except ImportError:
    print("[ERROR] Modul 'paramiko' tidak ditemukan. Install dengan: pip install paramiko")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.text import Text
    from rich.columns import Columns
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    print("[INFO] Install 'rich' untuk tampilan lebih keren: pip install rich")

# ──────────────────────────────────────────────────────
#  KONSTANTA WARNA & CONSOLE
# ──────────────────────────────────────────────────────
console = Console(force_terminal=True) if HAS_RICH else None

BANNER = """
[bold cyan]
  +---------+  SERVER  +-----------+
   ___  ___  ____   _  __ ____ ____ 
  / __||  _|| __ \\ | | \\ V /| ___|| _ \\
  \\__ \\| |_ |    / | |  \\ / | _|  |   /
  |___/|___||_|\\_\\ |_|   V   |____||_|\\_\\
[/bold cyan]
[bold yellow]  ____  ____  ____
 / ___||  __||  __|
 \\___ \\| _|  | |
  ___) | |___| |___
 |____/|_____|_____|  [/bold yellow][dim]ANALYZER v1.0 by famousbytee[/dim]
"""

# ──────────────────────────────────────────────────────
#  KELAS SSH MANAGER
# ──────────────────────────────────────────────────────
class SSHManager:
    def __init__(self, host, port, username, password, sudo_pass=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sudo_pass = sudo_pass or password
        self.client = None

    def connect(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host, port=self.port,
            username=self.username, password=self.password,
            timeout=15, banner_timeout=15
        )
        return self

    def run(self, cmd, timeout=20, use_sudo=False):
        """Jalankan command, return (stdout, stderr, exit_code)"""
        if use_sudo:
            cmd = f"echo '{self.sudo_pass}' | sudo -S bash -c {repr(cmd)} 2>/dev/null"
        try:
            stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            code = stdout.channel.recv_exit_status()
            return out, err, code
        except Exception as e:
            return "", str(e), -1

    def sudo(self, cmd, timeout=20):
        return self.run(cmd, timeout=timeout, use_sudo=True)

    def close(self):
        if self.client:
            self.client.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()


# ──────────────────────────────────────────────────────
#  HELPER OUTPUT
# ──────────────────────────────────────────────────────
def print_section(title, emoji=""):
    if HAS_RICH:
        console.print(f"\n{Rule(f'[bold white]{emoji} {title}[/bold white]', style='cyan')}")
    else:
        print(f"\n{'='*60}")
        print(f"  {emoji} {title}")
        print('='*60)

def print_ok(msg):
    if HAS_RICH:
        console.print(f"  [bold green]✅[/bold green] {msg}")
    else:
        print(f"  [OK] {msg}")

def print_warn(msg):
    if HAS_RICH:
        console.print(f"  [bold yellow]⚠️ [/bold yellow] {msg}")
    else:
        print(f"  [WARN] {msg}")

def print_danger(msg):
    if HAS_RICH:
        console.print(f"  [bold red]🚨[/bold red] {msg}")
    else:
        print(f"  [DANGER] {msg}")

def print_info(msg):
    if HAS_RICH:
        console.print(f"  [bold blue]ℹ️ [/bold blue] {msg}")
    else:
        print(f"  [INFO] {msg}")

def kv(key, val, color="white"):
    if HAS_RICH:
        console.print(f"  [dim]{key:<25}[/dim] [{color}]{val}[/{color}]")
    else:
        print(f"  {key:<25} {val}")


# ──────────────────────────────────────────────────────
#  MODUL ANALISIS
# ──────────────────────────────────────────────────────

class ServerAnalyzer:
    def __init__(self, ssh: SSHManager):
        self.ssh = ssh
        self.findings = []  # (level, message)

    def flag(self, level, msg):
        """Tambah temuan: level = CRITICAL / WARNING / INFO / OK"""
        self.findings.append((level, msg))

    # ── 1. SYSTEM INFO ──────────────────────────────────
    def analyze_system(self):
        print_section("INFORMASI SISTEM", "🖥️")

        out, _, _ = self.ssh.run("uname -a")
        kv("Kernel", out)

        out, _, _ = self.ssh.run("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'")
        kv("OS", out, "cyan")

        out, _, _ = self.ssh.run("hostname -f 2>/dev/null || hostname")
        kv("Hostname", out, "cyan")

        out, _, _ = self.ssh.run("uptime -p 2>/dev/null || uptime")
        kv("Uptime", out)

        out, _, _ = self.ssh.run("uptime | awk -F'load average:' '{print $2}' | xargs")
        loads = [x.strip().rstrip(',') for x in out.split(',')]
        load_color = "green"
        if loads:
            try:
                l1 = float(loads[0].replace(',', '.'))
                if l1 > 10:
                    load_color = "red"
                    self.flag("WARNING", f"Load average sangat tinggi: {out}")
                elif l1 > 5:
                    load_color = "yellow"
            except:
                pass
        kv("Load Average", out, load_color)

        out, _, _ = self.ssh.run("free -h | grep Mem | awk '{print $2\" total, \"$3\" used, \"$4\" free\"}'")
        kv("RAM", out)

        out, _, _ = self.ssh.run("df -h / | tail -1 | awk '{print $2\" total, \"$3\" used, \"$4\" free (\"$5\")\"}'")
        usage = re.search(r'(\d+)%', out)
        disk_color = "green"
        if usage:
            pct = int(usage.group(1))
            if pct >= 90:
                disk_color = "red"
                self.flag("WARNING", f"Disk hampir penuh: {pct}%")
            elif pct >= 75:
                disk_color = "yellow"
        kv("Disk /", out, disk_color)

        out, _, _ = self.ssh.run("nproc")
        kv("CPU Cores", out)

        out, _, _ = self.ssh.run("cat /proc/cpuinfo | grep 'model name' | head -1 | cut -d: -f2 | xargs")
        kv("CPU Model", out)

        out, _, _ = self.ssh.run("date")
        kv("Server Time", out)

    # ── 2. USER & SESSION ───────────────────────────────
    def analyze_users(self):
        print_section("PENGGUNA & SESI AKTIF", "👤")

        out, _, _ = self.ssh.run("who")
        if out:
            if HAS_RICH:
                t = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
                t.add_column("User"); t.add_column("Terminal")
                t.add_column("Login Time"); t.add_column("From IP")
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        ip = parts[-1].strip('()') if '(' in line else "-"
                        t.add_row(parts[0], parts[1], f"{parts[2]} {parts[3]}", ip)
                console.print(t)
            else:
                print(out)
        else:
            print_info("Tidak ada user lain yang login")

        # Last logins
        print_info("Riwayat login terakhir (10):")
        out, _, _ = self.ssh.run("last -n 10 | head -12")
        if HAS_RICH:
            console.print(f"[dim]{out}[/dim]")
        else:
            print(out)

        # Users with shell
        out, _, _ = self.ssh.sudo("cat /etc/passwd | awk -F: '$7 ~ /bash|zsh|sh$/ && $7 !~ /false|nologin/ {print $1\":\"$3\":\"$6\":\"$7}'")
        if out:
            print_info("Akun dengan shell login:")
            if HAS_RICH:
                t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
                t.add_column("Username"); t.add_column("UID"); t.add_column("Home"); t.add_column("Shell")
                for line in out.splitlines():
                    parts = line.split(':')
                    if len(parts) == 4:
                        uid_color = "red" if parts[1] == "0" else "white"
                        t.add_row(f"[{uid_color}]{parts[0]}[/{uid_color}]", parts[1], parts[2], parts[3])
                console.print(t)
            else:
                print(out)

        # Sudo users
        out, _, _ = self.ssh.sudo("getent group sudo wheel 2>/dev/null | cut -d: -f4")
        if out:
            print_warn(f"Anggota grup sudo/wheel: {out}")
            self.flag("INFO", f"Grup sudo: {out}")

    # ── 3. SERANGAN & AUTH LOG ──────────────────────────
    def analyze_attacks(self):
        print_section("ANALISIS SERANGAN", "🚨")

        # Failed logins - SSH
        out, _, _ = self.ssh.sudo(
            "grep -E 'Failed password|Invalid user|authentication failure' "
            "/var/log/auth.log 2>/dev/null | tail -200"
        )
        if not out:
            out, _, _ = self.ssh.sudo(
                "grep -E 'Failed|Invalid' /var/log/secure 2>/dev/null | tail -200"
            )

        ssh_attackers = Counter()
        ssh_users = Counter()
        ssh_lines = []
        if out:
            for line in out.splitlines():
                ip_match = re.search(r'from (\d+\.\d+\.\d+\.\d+)', line)
                user_match = re.search(r'(?:user|for) (\S+)', line)
                if ip_match:
                    ssh_attackers[ip_match.group(1)] += 1
                if user_match:
                    ssh_users[user_match.group(1)] += 1
                ssh_lines.append(line)

        if ssh_attackers:
            print_danger(f"Ditemukan {sum(ssh_attackers.values())} percobaan login SSH gagal!")
            if HAS_RICH:
                t = Table(title="Top IP Penyerang SSH", box=box.ROUNDED,
                          show_header=True, header_style="bold red")
                t.add_column("IP Penyerang", style="red")
                t.add_column("Percobaan", justify="right")
                t.add_column("Status", justify="center")
                for ip, count in ssh_attackers.most_common(10):
                    status = "🔴 KRITIS" if count >= 50 else "🟡 TINGGI" if count >= 10 else "🟠 SEDANG"
                    t.add_row(ip, str(count), status)
                console.print(t)
            else:
                for ip, count in ssh_attackers.most_common(10):
                    print(f"  {ip:<20} {count} percobaan")

            if HAS_RICH:
                t2 = Table(title="Username yang Diserang", box=box.SIMPLE,
                           show_header=True, header_style="bold yellow")
                t2.add_column("Username"); t2.add_column("Percobaan", justify="right")
                for u, c in ssh_users.most_common(10):
                    t2.add_row(u, str(c))
                console.print(t2)

            for ip, count in ssh_attackers.most_common(3):
                self.flag("CRITICAL", f"Brute force SSH dari {ip} — {count} percobaan")
        else:
            print_ok("Tidak ada percobaan login SSH gagal ditemukan")

        # Cockpit attacks
        out_c, _, _ = self.ssh.sudo(
            "grep -E 'cockpit.*authentication failure|cockpit.*auth.*failure' "
            "/var/log/auth.log 2>/dev/null | tail -100"
        )
        cockpit_ips = Counter()
        if out_c:
            for line in out_c.splitlines():
                ip_match = re.search(r'rhost=::ffff:(\d+\.\d+\.\d+\.\d+)|rhost=(\d+\.\d+\.\d+\.\d+)', line)
                if ip_match:
                    ip = ip_match.group(1) or ip_match.group(2)
                    cockpit_ips[ip] += 1
            if cockpit_ips:
                print_danger(f"Serangan Brute Force COCKPIT terdeteksi!")
                for ip, count in cockpit_ips.most_common(5):
                    print_danger(f"  IP: {ip} — {count} percobaan")
                    self.flag("CRITICAL", f"Brute force Cockpit dari {ip} — {count}x")

        # Webmin attacks
        out_w, _, _ = self.ssh.sudo(
            "grep -E 'webmin.*failure|usermin.*failure' /var/log/auth.log 2>/dev/null | tail -50"
        )
        if out_w:
            print_warn(f"Percobaan login Webmin/Usermin terdeteksi:\n{out_w[:300]}")
            self.flag("WARNING", "Percobaan login Webmin/Usermin gagal ditemukan")

        # Successful logins from unknown IPs
        out_ok, _, _ = self.ssh.sudo(
            "grep 'Accepted password' /var/log/auth.log 2>/dev/null | tail -20"
        )
        if out_ok:
            print_info("Login SSH berhasil (terakhir):")
            if HAS_RICH:
                console.print(f"[green]{out_ok}[/green]")
            else:
                print(out_ok)

        # Ban list from fail2ban
        out_f, _, _ = self.ssh.sudo("fail2ban-client status 2>/dev/null")
        if out_f:
            print_info("Status Fail2ban:")
            out_jails, _, _ = self.ssh.sudo(
                "fail2ban-client status 2>/dev/null | grep 'Jail list' | cut -d: -f2"
            )
            jails = [j.strip() for j in out_jails.split(',') if j.strip()]
            for jail in jails[:5]:
                out_jail, _, _ = self.ssh.sudo(f"fail2ban-client status {jail} 2>/dev/null | grep -E 'Currently banned|Total banned'")
                if out_jail:
                    print_info(f"  [{jail}] {out_jail.strip()}")

    # ── 4. PROSES & CPU ─────────────────────────────────
    def analyze_processes(self):
        print_section("PROSES AKTIF (TOP 15)", "⚙️")
        out, _, _ = self.ssh.run(
            "ps aux --sort=-%cpu 2>/dev/null | head -16 || ps aux | sort -k3 -rn | head -16"
        )
        lines = out.splitlines()
        if HAS_RICH:
            t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            t.add_column("USER", min_width=10)
            t.add_column("PID", justify="right")
            t.add_column("CPU%", justify="right")
            t.add_column("MEM%", justify="right")
            t.add_column("COMMAND", max_width=50)
            for line in lines[1:]:
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    cpu = float(parts[2]) if parts[2].replace('.','').isdigit() else 0
                    cpu_color = "red" if cpu > 50 else "yellow" if cpu > 20 else "white"
                    t.add_row(
                        parts[0], parts[1],
                        f"[{cpu_color}]{parts[2]}[/{cpu_color}]",
                        parts[3], parts[10][:50]
                    )
            console.print(t)
        else:
            print(out)

        # Suspicious processes
        out_sus, _, _ = self.ssh.run(
            "ps aux | grep -E 'miner|xmr|monero|cryptonight|stratum|minerd|cpuminer|kworker.*[0-9]{5}' "
            "| grep -v grep | head -10"
        )
        if out_sus:
            print_danger("PROSES MENCURIGAKAN (kemungkinan cryptominer)!")
            print_danger(out_sus)
            self.flag("CRITICAL", f"Proses mencurigakan ditemukan: {out_sus[:100]}")
        else:
            print_ok("Tidak ada proses cryptominer terdeteksi")

    # ── 5. NETWORK ──────────────────────────────────────
    def analyze_network(self):
        print_section("KONEKSI JARINGAN", "🌐")

        # Listening ports
        out, _, _ = self.ssh.run("ss -tlnp 2>/dev/null | grep LISTEN | sort -k4")
        if HAS_RICH:
            t = Table(title="Port yang Terbuka", box=box.ROUNDED,
                      show_header=True, header_style="bold green")
            t.add_column("Port"); t.add_column("Alamat"); t.add_column("Proses")
            dangerous_ports = {9090, 10000, 20000, 8080, 4444, 1337, 31337, 8088}
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    addr_port = parts[3]
                    addr, _, port_str = addr_port.rpartition(':')
                    try:
                        port = int(port_str)
                    except:
                        port = 0
                    proc = parts[-1] if len(parts) > 4 else "-"
                    color = "red" if port in dangerous_ports else "green"
                    flag = " ⚠️" if port in dangerous_ports else ""
                    t.add_row(f"[{color}]{port_str}{flag}[/{color}]", addr, proc)
            console.print(t)
        else:
            print(out)

        # Established connections
        out_e, _, _ = self.ssh.run("ss -tnp 2>/dev/null | grep ESTAB | head -20")
        if out_e:
            print_info("Koneksi aktif (ESTABLISHED):")
            if HAS_RICH:
                console.print(f"[dim]{out_e}[/dim]")
            else:
                print(out_e)

        # Suspicious outbound
        out_sus, _, _ = self.ssh.run(
            "ss -tnp 2>/dev/null | grep ESTAB | grep -vE ':(80|443|25|587|465|53|3306|5432|1990)\\s' | head -10"
        )
        if out_sus:
            print_warn("Koneksi keluar ke port tidak biasa:")
            print_warn(out_sus)

    # ── 6. FILE MENCURIGAKAN ────────────────────────────
    def analyze_files(self):
        print_section("FILE & FILESYSTEM MENCURIGAKAN", "📁")

        # Hidden files in /tmp
        out, _, _ = self.ssh.run("find /tmp /var/tmp /dev/shm -type f 2>/dev/null | head -30")
        if out:
            print_warn(f"File di /tmp, /var/tmp, /dev/shm:\n{out}")
            sus_files = [l for l in out.splitlines() if re.search(r'\.(sh|py|pl|elf|bin)$|^/tmp/\.', l)]
            if sus_files:
                for f in sus_files:
                    print_danger(f"File mencurigakan: {f}")
                    self.flag("CRITICAL", f"File mencurigakan di tmpfs: {f}")
        else:
            print_ok("Tidak ada file mencurigakan di /tmp")

        # SUID/SGID unusual
        out_suid, _, _ = self.ssh.sudo(
            "find / -perm /4000 -type f 2>/dev/null | grep -vE '/(bin|sbin|usr/bin|usr/sbin|usr/lib|snap)' | head -10"
        )
        if out_suid:
            print_warn(f"File SUID di lokasi tidak biasa:\n{out_suid}")
            self.flag("WARNING", "File SUID di luar direktori standar")
        else:
            print_ok("Tidak ada file SUID mencurigakan")

        # World-writable dirs
        out_ww, _, _ = self.ssh.sudo(
            "find /etc /var /usr -maxdepth 3 -perm -o+w -type f 2>/dev/null | head -10"
        )
        if out_ww:
            print_warn(f"File world-writable di sistem:\n{out_ww}")
        else:
            print_ok("Tidak ada file world-writable di /etc /var /usr")

        # Recently modified system files (last 24h)
        out_mod, _, _ = self.ssh.sudo(
            "find /etc /bin /sbin /usr/bin /usr/sbin -newer /etc/passwd -mtime -1 -type f 2>/dev/null | head -10"
        )
        if out_mod:
            print_warn(f"File sistem dimodifikasi dalam 24 jam terakhir:\n{out_mod}")
            self.flag("WARNING", "File sistem dimodifikasi baru-baru ini")
        else:
            print_ok("Tidak ada perubahan file sistem dalam 24 jam terakhir")

    # ── 7. SSH KEYS & BACKDOOR ──────────────────────────
    def analyze_ssh_backdoors(self):
        print_section("SSH KEYS & KEMUNGKINAN BACKDOOR", "🔑")

        # Check authorized_keys for all users
        out, _, _ = self.ssh.sudo(
            "for h in $(cut -d: -f6 /etc/passwd | sort -u); do "
            "f=\"$h/.ssh/authorized_keys\"; "
            "[ -f \"$f\" ] && echo \"=== $f ===\" && cat \"$f\"; done 2>/dev/null"
        )
        if out:
            print_warn("Ditemukan authorized_keys:")
            if HAS_RICH:
                console.print(Panel(out, title="authorized_keys", border_style="yellow"))
            else:
                print(out)
            key_count = out.count('ssh-')
            if key_count > 0:
                self.flag("WARNING", f"Ditemukan {key_count} SSH authorized key di sistem")
        else:
            print_ok("Tidak ada authorized_keys ditemukan")

        # Root SSH
        out_r, _, _ = self.ssh.sudo("cat /root/.ssh/authorized_keys 2>/dev/null")
        if out_r:
            print_danger(f"ROOT memiliki authorized_keys:\n{out_r}")
            self.flag("CRITICAL", "Root memiliki SSH authorized_keys — risiko backdoor!")

        # Check for .bash_history unusual commands
        out_h, _, _ = self.ssh.sudo(
            "tail -30 ~/.bash_history 2>/dev/null | grep -E 'curl|wget|nc|netcat|python.*-c|base64.*decode|chmod.*777' | head -10"
        )
        if out_h:
            print_warn(f"Perintah mencurigakan di history:\n{out_h}")
            self.flag("WARNING", "Perintah mencurigakan ditemukan di bash history")

    # ── 8. CRON & STARTUP ───────────────────────────────
    def analyze_cron(self):
        print_section("CRON JOBS & STARTUP", "📅")

        # System cron
        out, _, _ = self.ssh.sudo(
            "cat /etc/crontab 2>/dev/null; "
            "for f in /etc/cron.d/*; do echo \"==$f==\"; cat $f 2>/dev/null; done"
        )
        # User crons
        out_u, _, _ = self.ssh.sudo(
            "for user in $(cut -d: -f1 /etc/passwd); do "
            "crontab_u=$(crontab -u $user -l 2>/dev/null | grep -v '^#' | grep -v '^$'); "
            "[ -n \"$crontab_u\" ] && echo \"=== $user ===\" && echo \"$crontab_u\"; done"
        )
        if out_u:
            print_warn("Cron job user ditemukan:")
            # Check for suspicious
            sus_cron = [l for l in out_u.splitlines() if re.search(r'curl|wget|nc |bash -|python -c|/tmp/', l)]
            if sus_cron:
                for l in sus_cron:
                    print_danger(f"Cron mencurigakan: {l}")
                    self.flag("CRITICAL", f"Cron job mencurigakan: {l}")
            else:
                if HAS_RICH:
                    console.print(f"[dim]{out_u[:500]}[/dim]")
                else:
                    print(out_u[:500])
        else:
            print_ok("Tidak ada cron job user yang aktif")

        # Systemd unusual services
        out_s, _, _ = self.ssh.sudo(
            "systemctl list-units --type=service --state=running --no-legend 2>/dev/null | "
            "grep -vE '(systemd|ssh|snap|apt|cron|dbus|network|apache|nginx|mysql|mariadb|postgresql|"
            "fail2ban|ufw|rsyslog|postfix|dovecot|janus|coturn|webmin|cockpit|avahi|cups)' | head -15"
        )
        if out_s:
            print_warn("Layanan aktif yang tidak biasa:")
            if HAS_RICH:
                console.print(f"[yellow]{out_s}[/yellow]")
            else:
                print(out_s)

    # ── 9. FIREWALL ─────────────────────────────────────
    def analyze_firewall(self):
        print_section("FIREWALL & KEAMANAN", "🛡️")

        # UFW
        out_ufw, _, _ = self.ssh.sudo("ufw status verbose 2>/dev/null")
        if out_ufw and "inactive" not in out_ufw.lower():
            print_ok("UFW aktif:")
            if HAS_RICH:
                console.print(Panel(out_ufw[:500], title="UFW Status", border_style="green"))
            else:
                print(out_ufw[:500])
        elif out_ufw and "inactive" in out_ufw.lower():
            print_danger("UFW TIDAK AKTIF!")
            self.flag("CRITICAL", "Firewall UFW tidak aktif!")
        else:
            # Try iptables
            out_ipt, _, _ = self.ssh.sudo("iptables -L INPUT -n --line-numbers 2>/dev/null | head -20")
            if out_ipt:
                print_info("Iptables rules:")
                if HAS_RICH:
                    console.print(f"[dim]{out_ipt}[/dim]")
                else:
                    print(out_ipt)
            else:
                print_warn("Tidak bisa memeriksa firewall")

        # Fail2ban
        out_f2b, _, _ = self.ssh.sudo("fail2ban-client status 2>/dev/null | head -5")
        if out_f2b and "error" not in out_f2b.lower():
            print_ok(f"Fail2ban aktif: {out_f2b.splitlines()[0] if out_f2b else ''}")
        else:
            print_warn("Fail2ban tidak aktif atau tidak terinstal")
            self.flag("WARNING", "Fail2ban tidak aktif")

    # ── 10. PACKAGE & UPDATE ────────────────────────────
    def analyze_updates(self):
        print_section("UPDATE & PAKET KEAMANAN", "📦")

        out, _, _ = self.ssh.sudo(
            "apt list --upgradable 2>/dev/null | grep -i security | wc -l"
        )
        if out.isdigit() and int(out) > 0:
            print_warn(f"Ada {out} security update yang belum diinstall!")
            self.flag("WARNING", f"{out} security update tersedia")
            out2, _, _ = self.ssh.sudo(
                "apt list --upgradable 2>/dev/null | grep -i security | head -10"
            )
            if out2 and HAS_RICH:
                console.print(f"[yellow]{out2}[/yellow]")
        else:
            print_ok("Tidak ada security update tertunda (atau apt tidak tersedia)")

        # Check installed security tools
        tools = ['fail2ban', 'rkhunter', 'chkrootkit', 'lynis', 'aide', 'auditd']
        present = []
        missing = []
        for tool in tools:
            o, _, c = self.ssh.run(f"which {tool} 2>/dev/null || dpkg -l {tool} 2>/dev/null | grep '^ii'")
            if o:
                present.append(tool)
            else:
                missing.append(tool)
        if present:
            print_ok(f"Tools keamanan terinstall: {', '.join(present)}")
        if missing:
            print_warn(f"Tools keamanan tidak terinstall: {', '.join(missing)}")

    # ── RINGKASAN ────────────────────────────────────────
    def print_summary(self):
        print_section("RINGKASAN TEMUAN", "📊")

        critical = [(l, m) for l, m in self.findings if l == "CRITICAL"]
        warnings = [(l, m) for l, m in self.findings if l == "WARNING"]
        infos    = [(l, m) for l, m in self.findings if l == "INFO"]

        if HAS_RICH:
            if critical:
                console.print(f"\n[bold red]🔴 KRITIS ({len(critical)} temuan):[/bold red]")
                for _, msg in critical:
                    console.print(f"  [red]• {msg}[/red]")
            if warnings:
                console.print(f"\n[bold yellow]🟡 PERINGATAN ({len(warnings)} temuan):[/bold yellow]")
                for _, msg in warnings:
                    console.print(f"  [yellow]• {msg}[/yellow]")
            if infos:
                console.print(f"\n[bold blue]🔵 INFO ({len(infos)} temuan):[/bold blue]")
                for _, msg in infos:
                    console.print(f"  [blue]• {msg}[/blue]")
            if not self.findings:
                console.print("[bold green]✅ Tidak ada temuan keamanan serius![/bold green]")

            # Score
            score = 100 - (len(critical) * 20) - (len(warnings) * 5)
            score = max(0, score)
            color = "red" if score < 40 else "yellow" if score < 70 else "green"
            console.print(f"\n[bold]Skor Keamanan: [{color}]{score}/100[/{color}][/bold]")
        else:
            print(f"\n[KRITIS] {len(critical)} temuan")
            for _, m in critical:
                print(f"  • {m}")
            print(f"[PERINGATAN] {len(warnings)} temuan")
            for _, m in warnings:
                print(f"  • {m}")

    def run_all(self, modules=None):
        """Jalankan semua atau modul tertentu"""
        all_modules = {
            "system":    self.analyze_system,
            "users":     self.analyze_users,
            "attacks":   self.analyze_attacks,
            "processes": self.analyze_processes,
            "network":   self.analyze_network,
            "files":     self.analyze_files,
            "ssh":       self.analyze_ssh_backdoors,
            "cron":      self.analyze_cron,
            "firewall":  self.analyze_firewall,
            "updates":   self.analyze_updates,
        }
        to_run = modules if modules else list(all_modules.keys())
        for name in to_run:
            if name in all_modules:
                try:
                    all_modules[name]()
                except Exception as e:
                    print_warn(f"Modul '{name}' error: {e}")
        self.print_summary()


# ──────────────────────────────────────────────────────
#  AKSI TAMBAHAN
# ──────────────────────────────────────────────────────
def action_block_ip(ssh: SSHManager, ip: str):
    """Blokir IP dengan UFW"""
    print_section(f"BLOKIR IP: {ip}", "🚫")
    # Validasi IP
    try:
        socket.inet_aton(ip)
    except:
        print_danger(f"IP tidak valid: {ip}")
        return

    # Cek apakah sudah diblokir
    out, _, _ = ssh.sudo(f"ufw status | grep {ip}")
    if "DENY" in out:
        print_warn(f"IP {ip} sudah diblokir sebelumnya")
        return

    out, err, code = ssh.sudo(f"ufw deny from {ip} comment 'Blocked by server-analyzer'")
    if code == 0 or "Rule added" in out:
        print_ok(f"IP {ip} berhasil diblokir!")
        print_info("Menjalankan: sudo ufw reload...")
        ssh.sudo("ufw reload")
        print_ok("Firewall di-reload")
    else:
        print_danger(f"Gagal memblokir: {err or out}")
        # Fallback to iptables
        print_info("Mencoba dengan iptables...")
        out2, _, code2 = ssh.sudo(f"iptables -I INPUT -s {ip} -j DROP")
        if code2 == 0:
            print_ok(f"IP {ip} diblokir via iptables!")
        else:
            print_danger(f"Gagal juga dengan iptables: {out2}")


def action_show_blocked(ssh: SSHManager):
    """Tampilkan IP yang diblokir"""
    print_section("IP YANG DIBLOKIR", "📋")
    out, _, _ = ssh.sudo("ufw status | grep DENY")
    if out:
        print(out)
    else:
        out2, _, _ = ssh.sudo("iptables -L INPUT -n | grep DROP")
        if out2:
            print(out2)
        else:
            print_info("Tidak ada IP yang diblokir")


def action_unblock_ip(ssh: SSHManager, ip: str):
    """Hapus blokir IP"""
    print_section(f"HAPUS BLOKIR IP: {ip}", "✅")
    out, _, code = ssh.sudo(f"ufw delete deny from {ip}")
    if code == 0:
        print_ok(f"Blokir IP {ip} dihapus!")
        ssh.sudo("ufw reload")
    else:
        print_warn(f"Tidak bisa hapus via UFW, mencoba iptables...")
        ssh.sudo(f"iptables -D INPUT -s {ip} -j DROP")


def action_quick_harden(ssh: SSHManager):
    """Hardening cepat"""
    print_section("HARDENING CEPAT", "🔒")
    steps = [
        ("Aktifkan UFW", "ufw --force enable"),
        ("Blokir port Cockpit dari publik", "ufw deny 9090"),
        ("Blokir Webmin publik", "ufw deny 10000"),
        ("Blokir Usermin publik", "ufw deny 20000"),
        ("Pastikan SSH rate limit", "ufw limit 1990/tcp"),
        ("Reload UFW", "ufw reload"),
    ]
    for desc, cmd in steps:
        out, err, code = ssh.sudo(cmd)
        if code == 0 or "Rule" in out or "Skipping" in out:
            print_ok(desc)
        else:
            print_warn(f"{desc}: {err or out}")


# ──────────────────────────────────────────────────────
#  MAIN CLI
# ──────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="🔐 Server Security Analyzer — Audit keamanan server via SSH",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Contoh penggunaan:
  # Audit lengkap:
  python server_analyzer.py --host 103.210.121.29 --port 1990 --user faaris --password 090906sept

  # Hanya cek serangan:
  python server_analyzer.py -H 103.210.121.29 -P 1990 -u faaris -p 090906sept -m attacks

  # Beberapa modul:
  python server_analyzer.py -H 103.210.121.29 -P 1990 -u faaris -p 090906sept -m attacks,network,firewall

  # Blokir IP penyerang:
  python server_analyzer.py -H 103.210.121.29 -P 1990 -u faaris -p 090906sept --block-ip 62.60.130.237

  # Hapus blokir IP:
  python server_analyzer.py -H 103.210.121.29 -P 1990 -u faaris -p 090906sept --unblock-ip 62.60.130.237

  # Hardening cepat:
  python server_analyzer.py -H 103.210.121.29 -P 1990 -u faaris -p 090906sept --harden

  # Lihat IP yang diblokir:
  python server_analyzer.py -H 103.210.121.29 -P 1990 -u faaris -p 090906sept --show-blocked

Modul tersedia:
  system, users, attacks, processes, network, files, ssh, cron, firewall, updates
        """
    )

    conn = parser.add_argument_group("Koneksi SSH")
    conn.add_argument("-H", "--host", required=True, help="IP/hostname server")
    conn.add_argument("-P", "--port", type=int, default=22, help="Port SSH (default: 22)")
    conn.add_argument("-u", "--user", required=True, help="Username SSH")
    conn.add_argument("-p", "--password", required=True, help="Password SSH")
    conn.add_argument("--sudo-pass", help="Password sudo (default: sama dengan password SSH)")

    action = parser.add_argument_group("Aksi")
    action.add_argument("-m", "--module", default="all",
                        help="Modul: all / system,attacks,network,... (pisahkan koma)")
    action.add_argument("--block-ip", metavar="IP", help="Blokir IP tertentu")
    action.add_argument("--unblock-ip", metavar="IP", help="Hapus blokir IP")
    action.add_argument("--show-blocked", action="store_true", help="Tampilkan IP yang diblokir")
    action.add_argument("--harden", action="store_true", help="Jalankan hardening cepat")
    action.add_argument("--no-banner", action="store_true", help="Sembunyikan banner")

    return parser.parse_args()


def main():
    args = parse_args()

    if HAS_RICH and not args.no_banner:
        console.print(BANNER)
        console.print(Panel(
            f"[bold]Host:[/bold] [cyan]{args.host}:{args.port}[/cyan]\n"
            f"[bold]User:[/bold] [cyan]{args.user}[/cyan]\n"
            f"[bold]Waktu:[/bold] [cyan]{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]",
            title="[bold white]Target Server[/bold white]",
            border_style="cyan"
        ))

    ssh = SSHManager(
        host=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
        sudo_pass=args.sudo_pass
    )

    try:
        if HAS_RICH:
            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                transient=True
            ) as progress:
                task = progress.add_task("Menghubungkan ke server...", total=None)
                ssh.connect()
                progress.update(task, description="[green]Terhubung!")
        else:
            print(f"[*] Menghubungkan ke {args.host}:{args.port}...")
            ssh.connect()
            print("[+] Terhubung!")

    except paramiko.AuthenticationException:
        print_danger("Autentikasi gagal! Periksa username/password.")
        sys.exit(1)
    except (paramiko.SSHException, socket.error) as e:
        print_danger(f"Gagal konek: {e}")
        sys.exit(1)

    try:
        # Pilih aksi
        if args.block_ip:
            action_block_ip(ssh, args.block_ip)

        elif args.unblock_ip:
            action_unblock_ip(ssh, args.unblock_ip)

        elif args.show_blocked:
            action_show_blocked(ssh)

        elif args.harden:
            action_quick_harden(ssh)

        else:
            # Jalankan analisis
            analyzer = ServerAnalyzer(ssh)
            if args.module == "all":
                modules = None
            else:
                modules = [m.strip() for m in args.module.split(',')]
            analyzer.run_all(modules)

    except KeyboardInterrupt:
        if HAS_RICH:
            console.print("\n[yellow]⚡ Dibatalkan oleh user.[/yellow]")
        else:
            print("\n[!] Dibatalkan.")
    finally:
        ssh.close()

    if HAS_RICH:
        console.print(f"\n[dim]Selesai pada {datetime.datetime.now().strftime('%H:%M:%S')}[/dim]\n")


if __name__ == "__main__":
    main()
