"""Offline tests. Only synthetic arrays / explicitly provided public test images.
Run: python tests/test_eyes.py [--images /tmp/birdcal --preview /absolute/file.jpg]
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BIRDDESK_DATA_DIR"] = tempfile.mkdtemp(prefix="bd-eye-tests-")

import cv2
import numpy as np
from PIL import Image, ImageDraw
from app import analyzer, eyes


def pose(x=40, y=40, c=.98, conf=.95, right_c=0):
    pts = [[30,40,.95], [x,y,c], [x+20,y,right_c], [40,20,.9], [50,70,.8], [60,70,.8]]
    return {"box": [0,0,100,100], "confidence": conf, "keypoints": pts}


def flip(p, width=100):
    pts = np.array(p["keypoints"], dtype=float)
    pts[:,0] = width-pts[:,0]
    pts = pts[[0,2,1,3,5,4]]
    return {"box": [0,0,100,100], "confidence": p["confidence"], "keypoints": pts.tolist()}


def reconcile(a, b, mask=None):
    with patch.object(eyes,'refine_pupil',side_effect=lambda g,x,y,l:(x,y,True)):
        return eyes.reconcile(a,b,(50,30),100,(180,220),(50,30,150,130),
                              np.random.default_rng(0).random((180,220)).astype(np.float32),mask)


class EyeTests(unittest.TestCase):
    def test_coordinate_roundtrip(self):
        a=pose(); r=reconcile(a,flip(a))
        self.assertEqual(r["status"],"detected")
        self.assertEqual(r["selected"],0)
        self.assertAlmostEqual(r["eyes"][0]["x"],90/220,places=5)
        self.assertAlmostEqual(r["eyes"][0]["y"],70/180,places=5)
        self.assertEqual(r["eyes"][0]["side"],"left")
        self.assertAlmostEqual(r["eyes"][0]["agreement_px"],0)

    def test_two_eyes_and_flip_side(self):
        a=pose(right_c=.96); r=reconcile(a,flip(a))
        self.assertEqual(len(r["eyes"]),2)
        self.assertTrue(all(e["stable"] for e in r["eyes"]))

    def test_disagreement_not_actionable(self):
        a=pose(); b=flip(a); b["keypoints"][2][0]-=30
        r=reconcile(a,b)
        self.assertEqual(r["status"],"uncertain")
        self.assertIsNone(r["selected"])
        self.assertIsNone(r["eyes"][0]["sharpness"]["value"])

    def test_hidden_and_missing(self):
        r=reconcile(pose(c=.2),None)
        self.assertEqual(r["status"],"not_detected")
        self.assertEqual(r["eyes"],[])
        self.assertEqual(reconcile(None,None)["eyes"],[])

    def test_outside_mask_uncertain(self):
        a=pose();r=reconcile(a,flip(a),np.zeros((180,220),np.uint8))
        self.assertEqual(r["status"],"uncertain")

    def test_pupil_refinement_and_flat_abstention(self):
        g=np.full((200,200),.8,np.float32)
        cv2.circle(g,(105,95),12,.08,-1)
        x,y,ok=eyes.refine_pupil(g,100,100,400)
        self.assertTrue(ok)
        self.assertLess(np.hypot(x-105,y-95),3)
        self.assertFalse(eyes.refine_pupil(np.full_like(g,.8),100,100,400)[2])

    def test_pose_agreement_alone_is_not_confirmation(self):
        a=pose();r=eyes.reconcile(a,flip(a),(50,30),100,(180,220),(50,30,150,130),np.full((180,220),.8,np.float32))
        self.assertEqual(r['status'],'uncertain')
        self.assertIsNone(r['selected'])

    def test_subject_association(self):
        target=pose();neighbor=pose(conf=.99);neighbor["box"]=[200,200,300,300]
        self.assertIs(eyes.choose_pose([neighbor,target],[0,0,100,100]),target)
        self.assertIsNone(eyes.choose_pose([neighbor],[0,0,100,100]))

    def test_missing_weights_and_tiny_subject(self):
        with patch.object(eyes,"MODEL_PATH",Path('/missing-test-eye-model.pt')):
            loc=eyes.EyeLocator()
            rgb=np.zeros((200,200,3),np.uint8)
            self.assertEqual(loc.locate(rgb,[10,10,40,40])["status"],"too_small")
            self.assertEqual(loc.locate(rgb,[20,20,180,180])["status"],"unavailable")

    def test_bad_hash_no_unpickle(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'invalid.pt';path.write_bytes(b'not a checkpoint')
            with patch.object(eyes,"MODEL_PATH",path):
                loc=eyes.EyeLocator();loc._load()
                self.assertIsNone(loc.model)
                self.assertIn('校验失败',loc.load_error)

    def test_sharpness_degrades(self):
        n=100
        x,y=np.meshgrid(np.arange(n),np.arange(n))
        g=((x//5+y//5)%2).astype(np.float32)
        soft=cv2.GaussianBlur(g,(0,0),2)
        a=eyes.eye_sharpness(g,[20,20,80,80]);b=eyes.eye_sharpness(soft,[20,20,80,80])
        self.assertTrue(a["usable"] and b["usable"])
        self.assertGreater(a["value"],b["value"])
        self.assertFalse(eyes.eye_sharpness(np.zeros((20,20),np.float32),[0,0,20,20])["usable"])

    def test_no_bird_not_inferred(self):
        class Det:
            def detect(self,rgb): return []
        r=analyzer.analyze_image(Det(),np.zeros((200,200,3),np.uint8),{})
        self.assertEqual(r["eye"]["status"],"no_bird")

    def test_eye_error_keeps_body(self):
        class Failed:
            def locate(self,*a): raise RuntimeError('test')
        class Det:
            eye_locator=Failed()
            def detect(self,rgb): return [{"box":[20,20,180,180],"conf":.9,"poly":None}]
        rgb=np.random.default_rng(4).integers(0,255,(200,200,3),dtype=np.uint8)
        r=analyzer.analyze_image(Det(),rgb,{})
        self.assertEqual(r["eye"]["status"],"error")
        self.assertIsNotNone(r["focus"])

    def test_stop_does_not_lose_live_thread(self):
        class Live:
            def join(self,timeout): pass
            def is_alive(self): return True
        worker=analyzer.Analyzer(lambda *_:None);live=Live();worker.thread=live
        with self.assertRaises(RuntimeError): worker.stop()
        self.assertIs(worker.thread,live)
        self.assertTrue(worker.stop_flag.is_set())

    def test_body_rank_unchanged_by_eye(self):
        from app.server import LIB
        pid='test-eye-rank'; original=LIB.analysis.get(pid)
        try:
            value={"focus":{"sharp":.65,"energy":20,"bg":0,"anis":.9},
                   "birds":[{"area":.2,"edge":False}],"main":0}
            LIB.analysis[pid]=value;before=LIB.rank_value(pid)
            value['eye']={"status":"detected","eyes":[{"sharpness":{"value":0.1}}]}
            self.assertEqual(before,LIB.rank_value(pid))
        finally:
            if original is None:LIB.analysis.pop(pid,None)
            else: LIB.analysis[pid]=original

    def test_contract_survives_cache_and_api(self):
        from app.server import LIB
        from fastapi.testclient import TestClient
        from app.server import app
        p=pose();value={"v":analyzer.VERSION,"birds":[],"main":None,"focus":None,"eye":reconcile(p,flip(p))}
        analyzer._save([('cache-eye',value)])
        self.assertEqual(analyzer.load_cached(['cache-eye'])['cache-eye']['eye']['status'],'detected')
        value['eye']=eyes.empty_result('unavailable','missing')
        analyzer._save([('cache-eye',value)])
        self.assertEqual(analyzer.load_cached(['cache-eye']),{})
        c=TestClient(app)
        self.assertEqual(c.get('/api/env').status_code,200)


def public_smoke(src: Path, preview: Path | None):
    """This verifies inference contract; detection rate != localization accuracy."""
    paths=sorted(src.glob('b*.jpg'))
    if not paths:raise ValueError('No public test bird images')
    import socket
    def no_network(*args,**kwargs):
        raise AssertionError('Offline eye inference tried to access the network')
    # Block DNS and socket connection while importing/loading/running the models.
    with patch.object(socket,'getaddrinfo',no_network), patch.object(socket.socket,'connect',no_network):
        det=analyzer.Detector()
        first=cv2.imread(str(paths[0]))
        trial=analyzer.analyze_image(det,cv2.cvtColor(first,cv2.COLOR_BGR2RGB),{})
        assert trial['eye']['status'] not in {'error','unavailable'}
    stats={};rows=[];t0=time.monotonic()
    for p in paths:
        bgr=cv2.imread(str(p))
        if bgr is None:continue
        rgb=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB)
        a=analyzer.analyze_image(det,rgb,{})
        e=a['eye'];stats[e['status']]=stats.get(e['status'],0)+1
        assert all(0<=k['x']<=1 and 0<=k['y']<=1 for k in e['eyes'])
        assert (e['selected'] is not None) == (e['status']=='detected')
        rows.append((p.name,rgb,a))
        print(p.name,e['status'],[(x['side'],x['confidence'],x['agreement_px']) for x in e['eyes']])
    print('PUBLIC_SMOKE',json.dumps(stats),'total_seconds',round(time.monotonic()-t0,2))
    if not preview:return
    # Source and inset are same pixels; labels explicitly mark model, not truth.
    canvas=Image.new('RGB',(1200,((len(rows)+2)//3)*310),'#ffffff');d=ImageDraw.Draw(canvas)
    for n,(name,rgb,a) in enumerate(rows):
        xx=(n%3)*400;yy=(n//3)*310
        im=Image.fromarray(rgb);im.thumbnail((390,240));canvas.paste(im,(xx,yy+35))
        d.text((xx+4,yy+3),name+' | '+a['eye']['status'],fill='#111111')
        for e in a['eye']['eyes']:
            x,y=xx+e['x']*im.width,yy+35+e['y']*im.height
            color='#087d55' if e['stable'] else '#b87500'
            d.ellipse((x-5,y-5,x+5,y+5),outline=color,width=2)
        d.text((xx+4,yy+283),'Model candidates; not ground truth',fill='#555555')
    canvas.save(preview)
    insets=Image.new('RGB',(1000,((len(rows)+3)//4)*250),'#ffffff');di=ImageDraw.Draw(insets)
    for n,(name,rgb,a) in enumerate(rows):
        xx=(n%4)*250;yy=(n//4)*250;es=a['eye']['eyes']
        di.text((xx+3,yy+3),name+' '+a['eye']['status'],fill='#111111')
        if not es:continue
        e=es[a['eye']['selected'] or 0]
        h,w=rgb.shape[:2];x,y=e['x']*w,e['y']*h
        region=Image.fromarray(rgb).crop((round(x)-100,round(y)-100,round(x)+100,round(y)+100))
        insets.paste(region,(xx+25,yy+25))
        color='#087d55' if e['stable'] else '#b87500'
        # Draw offset cross hairs without hiding the pupil.
        for dx,dy,ex,ey in [(-16,0,-7,0),(7,0,16,0),(0,-16,0,-7),(0,7,0,16)]:
            di.line((xx+125+dx,yy+125+dy,xx+125+ex,yy+125+ey),fill=color,width=2)
        di.text((xx+3,yy+230),'Native pixels; predicted center',fill='#555555')
    insets.save(preview.with_name(preview.stem+'_insets.jpg'))


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--images',type=Path);ap.add_argument('--preview',type=Path)
    args=ap.parse_args()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EyeTests))
    if not result.wasSuccessful():raise SystemExit(1)
    if args.images:public_smoke(args.images,args.preview)
