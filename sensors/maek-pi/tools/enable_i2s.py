#!/usr/bin/env python3
"""Enable static SD/MODE MAX98357A wiring; back up boot configuration first."""
from datetime import datetime
import os
from pathlib import Path
import re
import shutil


def rewrite(text):
    output = []
    for line in text.splitlines():
        active = line.split('#', 1)[0].strip()
        if active.startswith('include '):
            raise ValueError('config.txt has include directives: configure the effective file manually using README.md')
        if active.startswith('dtoverlay='):
            overlay = active.split('=', 1)[1].split(',')[0].strip()
            if any(word in overlay.lower() for word in ('hifiberry', 'iqaudio', 'googlevoicehat', 'audioinjector', 'i2s-dac', 'adau')):
                raise ValueError('Another I2S audio overlay exists: ' + overlay)
            if overlay == 'max98357a':
                output.append('# maek previous: ' + line)
                continue
        if active == 'dtparam=audio=on':
            output.append('# maek previous: ' + line)
        else:
            output.append(line)
    output.extend(['', '[all]', '# Maek stereo MAX98357A; SD/MODE is resistor-selected, not GPIO-controlled.',
                   'dtparam=i2s=on', 'dtoverlay=max98357a,no-sdmode', ''])
    return '\n'.join(output)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo python3 tools/enable_i2s.py')
    model = Path('/proc/device-tree/model')
    if not model.exists() or 'Raspberry Pi' not in model.read_text():
        raise SystemExit('Raspberry Pi hardware not detected; no changes made')
    candidates = [Path('/boot/firmware/config.txt'), Path('/boot/config.txt')]
    config = next((p for p in candidates if p.is_file()), None)
    if config is None:
        raise SystemExit('No Pi boot config found. See manual instructions in README.md')
    overlay = config.parent / 'overlays/max98357a.dtbo'
    if not overlay.exists():
        raise SystemExit('max98357a.dtbo is missing. Check Pi kernel/firmware; no changes made')
    original = config.read_text()
    active_lines = [x.split('#',1)[0].strip() for x in original.splitlines()]
    if 'dtoverlay=max98357a,no-sdmode' in active_lines:
        print('Existing no-sdmode overlay found; unchanged. Verify active [all]/[pi3] section.')
        return
    updated = rewrite(original)
    backup = config.with_name(config.name + '.maek-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copy2(config, backup)
    # Pi boot filesystem may be FAT; preserve file rather than Unix ownership operations.
    with config.open('w') as f:
        f.write(updated)
        f.flush()
        os.fsync(f.fileno())
    print('Updated:', config, '\nBackup:', backup, '\nReboot required: sudo reboot')

if __name__ == '__main__':
    main()
