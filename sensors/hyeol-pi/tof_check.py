#!/usr/bin/env python3
"""혈 ToF(B0410) 잡음 점검 — 범위 모드별로 3초씩 찍어 유효 화소 · 잡음을 한 줄씩 출력하고 그림을 남긴다.
쓰기 (Pi, 수집단을 먼저 끔):  sudo systemctl stop ripples-collector
                               ~/tof-env/bin/python tof_check.py            # 2000, 4000 mm 모드 비교
                               ~/tof-env/bin/python tof_check.py 4000       # 한 모드만
끝나면 다시:                    sudo systemctl start ripples-collector
카메라는 1–2 m 앞 벽이나 상자를 향하게 두고 움직이지 않게."""
import sys, time
import numpy as np
import ArducamDepthCamera as ac

modes = [int(x) for x in sys.argv[1:]] or [2000, 4000]
cam = ac.ArducamCamera()
if cam.open(ac.Connection.CSI, 0) != 0: sys.exit("카메라 열기 실패")
if cam.start(ac.FrameType.DEPTH) != 0: sys.exit("카메라 시작 실패")
try:
    print(f"SDK {getattr(ac, '__version__', '?')} · 해상도 {cam.getCameraInfo().width}x{cam.getCameraInfo().height}")
    for mode in modes:
        cam.setControl(ac.Control.RANGE, mode)
        try: got = cam.getControl(ac.Control.RANGE)
        except Exception: got = "?"
        time.sleep(1.0)   # 모드 바뀐 뒤 안정
        D, C, t0 = [], [], time.time()
        while time.time() - t0 < 3:
            f = cam.requestFrame(2000)
            if f is None: continue
            D.append(np.array(f.depth_data, np.float32)); C.append(np.array(f.confidence_data, np.float32)); cam.releaseFrame(f)
        D, C = np.stack(D), np.stack(C); n = len(D)
        ok = (D > 0) & (C >= 30)
        stable = ok.all(0)
        std = float(np.median(D[:, stable].std(0))) if stable.sum() > 20 else float("nan")
        q500 = float(np.isin(np.rint(D[D > 0]).astype(int) % 500, [0, 205, 295]).mean())
        c = D[:, 85:95, 115:125]; c = c[c > 0]
        print(f"범위 {mode} (읽은 값 {got}): {n / 3:.0f} fps · 유효(conf≥30) {ok.mean() * 100:.0f}% · 신뢰도 중앙값 {np.median(C):.0f}"
              f" · 정지 화소 흔들림 {std:.0f} mm · 500 mm 단위 값 {q500 * 100:.0f}% · 가운데 {np.median(c) if c.size else 0:.0f} mm")
        np.savez_compressed(f"tof_check_{mode}.npz", depth=D[:10].astype(np.uint16), conf=np.minimum(C[:10], 255).astype(np.uint8))
    print("저장: tof_check_<범위>.npz (맥으로 보내 주면 비교 그림을 만듦)")
finally:
    cam.stop(); cam.close()
