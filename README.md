# Btrfs Restore TUI (AGY Time Explorer)

> **Herramienta TUI retro (fósforo verde) para exploración y restauración granular de archivos desde snapshots Btrfs locales y remotos.**

---

## 1. Visión General y Propósito

En entornos Linux con sistemas de archivos Btrfs (como Arch Linux / Omarchy), los backups de sistema y usuario se realizan mediante snapshots atómicos y transmisiones comprimidas (`btrfs send | zstd`). 

Aunque existen scripts para la creación de estos respaldos (como `backup-now`), la recuperación de un único archivo o carpeta borrada accidentalmente (por ejemplo, una nota de Obsidian o un archivo de configuración en `~/.config`) suele exigir comandos manuales de consola (`btrfs receive`, montajes temporales, permisos de `sudo`, etc.).

**Btrfs Restore TUI** resuelve este problema ofreciendo una interfaz en terminal (TUI) ligera, rápida y estética que emula la simplicidad de **Apple Time Machine** con la ergonomía clásica de los sistemas UNIX retro (estilo terminal serie VT100 / Midnight Commander).

---

## 2. Requerimientos Funcionales y Flujo de Usuario

### 2.1. Selección de Origen (Snapshot Selector)
Al iniciar la aplicación, se presenta una lista clara con las copias disponibles:
1. **Snapshots Locales (Instantáneos):**
   * Lee directamente de `/.snapshots/home_parent/` (o snapshots activos locales en la máquina).
   * Cero tiempo de espera; no requiere descargas ni montaje de streams.
2. **Snapshots Remotos (i7server / USB):**
   * Escanea el servidor `i7server` (`100.81.31.97:/mnt/SATA_3TB_B/dellomar_backups`) vía SSH o unidades USB conectadas (`/run/media/hbarchini/USB_SSD/dellomar_backups`).
   * Lista los archivos disponibles ordenados por fecha y hora (ej. `dellomar_home_20260906_033102.btrfs.zst`).
   * Al seleccionar uno remoto, la herramienta lo despliega de forma transparente en un subvolumen temporal (`/tmp/btrfs_restore_tmp/`) y desmonta/elimina el subvolumen automáticamente al salir.

### 2.2. Explorador de Archivos TUI (Keyboard-Driven)
* **Navegación:** Teclas de flecha `↑` y `↓` para recorrer carpetas y archivos.
* **Expandir/Entrar:** Tecla `Enter` para abrir carpetas o descender en el árbol.
* **Selección múltiple:** Barra espaciadora `Espacio` para marcar/desmarcar elementos (`[ ]` ➔ `[X]`).
* **Navegación entre paneles:** Tecla `Tab` para alternar entre el árbol de archivos y los botones de acción inferior.
* **Retroceder / Cancelar:** Tecla `Esc`.
* **Salir:** Tecla `q` o `Ctrl+C`.

### 2.3. Acciones de Restauración
* **Restaurar en Ubicación Original:**
  * Si el archivo original aún existe en el sistema actual, solicita confirmación:
    * `[ Sobrescribir ]`
    * `[ Mantener ambos (renombrar a .bak) ]`
    * `[ Cancelar ]`
* **Extraer en Ubicación Personalizada:**
  * Permite extraer los archivos marcados en una carpeta a elección (por defecto `~/Desktop/recuperados_YYYYMMDD/` o seleccionada mediante un navegador de carpetas).

---

## 3. Especificación Visual y Estética (Retro Fósforo Verde)

* **Paleta Base:** Fondo negro absoluto (`#000000`) con texto, marcos y bordes en **verde fósforo brillante** (`#00FF66` / `#33FF33`), emulando terminales clásicas VT100 / CRT.
* **Elementos Seleccionados (`[X]`):** Resaltado en **amarillo intenso / ámbar** (`#FFFF00` / `#FFD700` o texto en negrita amarilla sobre fondo verde oscuro) para máxima visibilidad de lo que se va a restaurar.
* **Feedback de Operación (Spinner y Porcentaje):**
  * Durante el proceso de copia/restauración de archivos o de descompresión remota con `zstd`:
    * Mostrar un **contador de progreso porcentual**: `[ 45% ]`
    * Acompañado del **spinner rotatorio clásico ASCII**: `[ | ]`, `[ / ]`, `[ - ]`, `[ \ ]`
    * Esto garantiza al usuario que el sistema está trabajando activamente y no se ha quedado congelado.

---

## 4. Mockup Visual de la TUI

```text
┌── [ Btrfs Restore TUI v1.0 - AGY Ecosystem ] ────────────────────────────────┐
│ Origen: [LOCAL] /.snapshots/home_parent (06-Sep-2026 03:51)                   │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  📁 .config/                                                                 │
│  ▼ 📁 Documents/                                                             │
│    ▼ 📁 desarrollo/                                                          │
│        [ ] 📄 README.md                                                      │
│    ▼ 📁 obsidian/                                                            │
│      ▼ 📁 ob_vault/                                                          │
│          [ ] 📄 Index_General_01-09-2026.md                                  │
│          [X] 📄 Directivas_Oficiales_05-09-2026.md   <-- (Amarillo Brillante)│
│          [X] 📄 Nota_Proyecto_06-09-2026.md          <-- (Amarillo Brillante)│
│  📁 Downloads/                                                               │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ 2 archivos seleccionados (48.6 KB)                                           │
│ Estado: [ / ] Restaurando archivos... 68%                                    │
├──────────────────────────────────────────────────────────────────────────────┤
│  [ <R> Restaurar en Original ]   [ <E> Extraer en... ]   [ <Q> Salir ]       │
└──────────────────────────────────────────────────────────────────────────────┘
 [↑/↓] Navegar   [Enter] Abrir   [Espacio] Seleccionar   [Tab] Acciones   [Esc] Atrás
```

---

## 5. Arquitectura Técnica y Stack

* **Lenguaje:** Python 3 (nativo en el sistema).
* **Librería TUI:** 
  * Opción principal recomendada: **`textual`** (creación de interfaces de terminal asíncronas con CSS, soporte completo de colores TrueColor, widgets de árbol `Tree`, manejo de eventos y barras de progreso fluidas).
  * Opción alternativa sin dependencias externas: **`curses`** o combinación con **`gum`** (ya preinstalado en Omarchy).
* **Manejo de Btrfs y SSH:**
  * `btrfs subvolume snapshot` y `btrfs subvolume delete` para montajes efímeros.
  * `zstd -d` y `btrfs receive` para procesamiento de flujos remotos.
  * Conexión SSH sin contraseña mediante llaves públicas preconfiguradas hacia `hbarchini@100.81.31.97`.

---

## 6. Estándar de Despliegue e Instalación (Regla 7 Ecosistema AGY)

Al finalizar la programación y pruebas:
1. El código fuente reside en `~/Documents/desarrollo/btrfs-restore-tui/`.
2. Se genera el script de instalación formal `install.sh` que:
   * Empaquete o copie los binarios y entorno virtual a `/opt/btrfs-restore-tui/`.
   * Cree el ejecutable symlink global en `/usr/local/bin/restore-now` (o `/usr/local/bin/btrfs-restore`).
   * Genere el acceso de escritorio en `/usr/share/applications/btrfs-restore.desktop` configurado con `Terminal=true` o ejecutando dentro de `foot`.
3. Esto asegura que limpiar o mover la carpeta de desarrollo no rompa la aplicación instalada en el sistema.

---

## 7. Decision Log

* **06-09-2026:**
  * **Decisión:** Descartar GUI tradicional (GTK/Qt) en favor de una TUI retro en terminal.
  * **Justificación:** Mayor estabilidad en Wayland/Hyprland, arranque instantáneo en `foot`, cero sobrecarga de dependencias pesadas y ergonomía superior de teclado.
  * **Decisión:** Selección en amarillo brillante y spinner con porcentaje durante la restauración.
  * **Justificación:** Retroalimentación visual inmediata que previene la ansiedad de que el sistema se haya colgado.
