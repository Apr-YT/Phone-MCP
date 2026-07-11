import sys, time
sys.path.insert(0, '.')
import server
DEV = '134d2f8'
w, h = server._screen_size(DEV)

server._wechat_ensure_home(DEV)
print('at home?', server._ocr_sees(DEV, '微信', region=[0,0,1,0.12]))
if not server._search_opened(DEV):
    server.run_adb(['shell','input','tap',str(int(w*0.83)),str(int(h*0.07))],device=DEV,mutating=True)
    time.sleep(0.5)
print('search opened?', server._search_opened(DEV))
server.run_adb(['shell','input','tap',str(int(w*0.5)),str(int(h*0.07))],device=DEV,mutating=True)
time.sleep(0.2)
r = server.t_input_text({'text':'向远钦','deviceSerial':DEV,'field':'search'})
print('input contact result:', r.get('success'), r.get('data',{}).get('method'), r.get('data',{}).get('verified'))
# OCR search region
boxes = server.ocr_boxes(DEV, region=[0,0.06,1,0.16], min_conf=0.2)
print('search region OCR:', [b[0] for b in boxes])
hits = server.ocr_match_contact('向远钦', DEV, region=[0,0.12,1,0.6])
print('match hits:', hits[:3])
if hits:
    _,cx,cy,_ = hits[0]
    print('clicking', cx, cy)
    server.run_adb(['shell','input','tap',str(cx),str(cy)],device=DEV,mutating=True)
    time.sleep(1.2)
    print('chat_header_is:', server._chat_header_is(DEV,'向远钦'))
    boxes2 = server.ocr_boxes(DEV, region=[0,0,1,0.15], min_conf=0.2)
    print('top OCR after click:', [b[0] for b in boxes2])
    print('ocr_sees 发消息:', server._ocr_sees(DEV,'发消息',region=[0,0.2,1,0.9]))
