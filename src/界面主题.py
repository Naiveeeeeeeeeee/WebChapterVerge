"""统一的原生 Tk 桌面配色；不依赖浏览器窗口。"""
from tkinter import ttk

BG = '#f2f5fa'
CARD = '#ffffff'
INK = '#172338'
MUTED = '#718097'
BLUE = '#4263eb'
NAV = '#152238'


def setup_theme(root):
    style = ttk.Style(root)
    style.theme_use('clam')
    root.configure(background=BG)
    style.configure('.', font=('PingFang SC', 12), background=BG, foreground=INK)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=CARD)
    style.configure('TLabel', background=BG, foreground=INK)
    style.configure('Card.TLabel', background=CARD)
    style.configure('Title.TLabel', font=('PingFang SC', 25, 'bold'))
    style.configure('CardTitle.TLabel', font=('PingFang SC', 14, 'bold'), background=CARD)
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('CardMuted.TLabel', foreground=MUTED, background=CARD)
    style.configure('TButton', background='#e8edf5', foreground=INK, borderwidth=0, padding=(14, 9), font=('PingFang SC', 12))
    style.map('TButton', background=[('active', '#dde5f2'), ('disabled', '#edf0f5')], foreground=[('disabled', '#a5afbf')])
    style.configure('Primary.TButton', background=BLUE, foreground='white', font=('PingFang SC', 12, 'bold'))
    style.map('Primary.TButton', background=[('active', '#3655d5'), ('disabled', '#c6ceef')], foreground=[('disabled', '#f3f5ff')])
    style.configure('TEntry', fieldbackground='white', bordercolor='#dce3ee', lightcolor='#dce3ee', darkcolor='#dce3ee', padding=8)
    style.map('TEntry', bordercolor=[('focus', BLUE)])
    style.configure('TCheckbutton', padding=4)
    style.configure('Horizontal.TProgressbar', background=BLUE, troughcolor='#e6ecf5', borderwidth=0, thickness=7)
    return style
