"""
Definición de estilos retro fósforo verde y constantes visuales para Btrfs Restore TUI.
"""

SPINNER_FRAMES = ["[ | ]", "[ / ]", "[ - ]", r"[ \ ]"]

RETRO_CSS = """
Screen {
    background: #000000;
    color: #00FF66;
}

Header {
    background: #001a0a;
    color: #00FF66;
    text-style: bold;
    border-bottom: heavy #00FF66;
}

Footer {
    background: #001a0a;
    color: #00FF66;
    border-top: heavy #00FF66;
}

#snapshot-bar {
    height: 3;
    padding: 0 1;
    background: #002200;
    border-bottom: solid #00FF66;
    color: #00FF66;
}

#tree-container {
    width: 100%;
    height: 1fr;
    border: double #00FF66;
    background: #000000;
    padding: 0 1;
}

Tree {
    background: #000000;
    color: #00FF66;
    scrollbar-color: #00FF66 #002200;
}

Tree:focus {
    border: none;
}

Tree > .tree--cursor {
    background: #003311;
    color: #00FF66;
    text-style: bold;
}

/* Items seleccionados en amarillo brillante */
.selected-item {
    color: #FFFF00;
    text-style: bold;
}

#status-box {
    height: 4;
    border: solid #00FF66;
    background: #001100;
    padding: 0 1;
    color: #00FF66;
}

#action-bar {
    height: 3;
    align: center middle;
    background: #000000;
    margin-top: 1;
}

Button {
    background: #002200;
    color: #00FF66;
    border: round #00FF66;
    margin: 0 1;
    min-width: 18;
    height: 3;
    content-align: center middle;
    text-style: bold;
}

Button:hover {
    background: #004400;
    color: #FFFF00;
    border: round #FFFF00;
}

Button:focus {
    background: #00FF66;
    color: #000000;
    border: round #FFFFFF;
    text-style: bold;
}

/* Diálogos modales retro */
ModalScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.85);
}

#modal-dialog {
    width: 78;
    max-height: 88%;
    height: auto;
    border: double #00FF66;
    background: #001100;
    padding: 1 2;
}

#modal-title {
    text-align: center;
    color: #FFFF00;
    text-style: bold;
    border-bottom: solid #00FF66;
    margin-bottom: 1;
}

#modal-content {
    color: #00FF66;
    margin-bottom: 1;
}

#modal-scroll-content {
    max-height: 16;
    height: auto;
    margin-bottom: 1;
    scrollbar-color: #00FF66 #002200;
}

.snap-btn {
    width: 100%;
    margin-bottom: 1;
    text-align: left;
}

#modal-buttons {
    align: center middle;
    height: auto;
    min-height: 3;
    margin-top: 1;
}

#progress-box {
    width: 76;
    height: auto;
    min-height: 15;
    border: double #00FF66;
    background: #001100;
    padding: 1 3;
    align: center middle;
}

#progress-spinner {
    text-align: center;
    color: #FFFF00;
    text-style: bold;
    margin-bottom: 1;
}

#progress-filename {
    text-align: center;
    color: #00FF66;
    margin-top: 1;
    margin-bottom: 1;
}

ProgressBar {
    width: 100%;
    margin-top: 1;
}

#btn-done {
    min-width: 22;
    width: auto;
    height: 3;
    background: #002200;
    color: #00FF66;
    border: round #00FF66;
    content-align: center middle;
    text-style: bold;
    margin: 0;
}

#btn-done:hover {
    background: #004400;
    color: #FFFF00;
    border: round #FFFF00;
}

#btn-done:focus {
    background: #00FF66 !important;
    color: #000000 !important;
    border: round #FFFFFF !important;
    text-style: bold;
}

ProgressBar > .bar--bar {
    color: #00FF66;
    background: #003300;
}

ProgressBar > .bar--complete {
    color: #FFFF00;
}
"""
