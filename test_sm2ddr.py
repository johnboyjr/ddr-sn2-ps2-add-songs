import struct, subprocess, tempfile, unittest, xml.etree.ElementTree as ET
from pathlib import Path
import sm2ddr as S

SM = """#TITLE:Test Song;#SUBTITLE:Mix;#ARTIST:Nobody;#MUSIC:song.ogg;
#OFFSET:0.5;#SAMPLESTART:2;#SAMPLELENGTH:3;
#BPMS:0=120,8=240;#STOPS:4=0.5;
#NOTES:
 dance-single:: Medium: 7: 0,0,0,0,0:
1000
0100
0010
0001
,
2000
0000
3000
1001
,
0000
0M00
0000
0000
;
#NOTES:
 dance-double:: Hard: 21: 0,0,0,0,0:
10000001
00000000
00000000
00000000
;
#NOTES:
 pump-single:: Hard: 9: 0,0,0,0,0:
10000
;
"""
class Args: tps=1000
AARO_XSB=bytes.fromhex("5344424b2b002b0020ce7dac269e1e40d7010102000000000010000102000c000000f6000000ffffffff2c010000ffffffffffffffffffffffff8a0000000001000020010000ca0000006161726f0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000006161726f000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100b40000000c00010000010100b4000000200001b4e500000001010000200000ff0c000000ff0000000004ca00000004d6000000ffffffffffffffffffffffffffffffff0100ffffffff0000ffffffffffffffff2c010000ffff31010000ffff6161726f006161726f5f7300")
class T(unittest.TestCase):
    @classmethod
    def setUpClass(c):
        c.tmp=tempfile.TemporaryDirectory(); c.d=Path(c.tmp.name)
        c.songs=c.d/'songs'/'Test'; c.songs.mkdir(parents=True)
        (c.songs/'t.sm').write_text(SM)
        subprocess.run('ffmpeg -y -v error -f lavfi -i sine=440:d=8 song.ogg'.split(),cwd=c.songs,check=True)
    def conv(c):
        s=S.parse_simfile(c.songs/'t.sm'); d,l,sh=S.convert_charts(s,Args); return s,S.read_ssq(d),l,sh,d
    def test_chart(c):
        s,ch,l,sh,_=c.conv()
        c.assertEqual(sh,4.5)
        c.assertEqual(l,[0,0,7,0,0, 0,0,0,19,0])
        sp=[x for x in ch if x['kind']==3 and x['p2']==0x0214][0]
        c.assertEqual(sp['offsets'],[8192+o for o in (0,1024,2048,3072,4096,6144,7168)])
        c.assertEqual(sp['steps'],[1,2,4,8,1,0,9])
        c.assertEqual(sp['extra'],[1,1])
        dp=[x for x in ch if x['kind']==3 and x['p2']==0x0318][0]
        c.assertEqual(dp['steps'],[0x81])
    def test_timing(c):
        _,ch,_,_,_=c.conv(); t=ch[0]
        o,k=t['offsets'],t['ticks']; c.assertEqual((o[0],k[0]),(0,0))
        bpms=[]
        for i in range(1,len(o)):
            if o[i]==o[i-1]: c.assertAlmostEqual((k[i]-k[i-1])/1000,.5); continue
            bpms.append(round((o[i]-o[i-1])/4096/((k[i]-k[i-1])/1000/240)))
        c.assertEqual(bpms,[120,120,120,240])
        c.assertEqual(o[:4],[0,8192,12288,12288]); c.assertEqual(k[:4],[0,4000,6000,6500])
    def test_alignment(c):
        d=c.conv()[4]; c.assertEqual(len(d)%4,0); c.assertEqual(d[-4:],b'\0'*4)
    def test_lz77_arc(c):
        comp=bytes([0x3F])+b'abcdef'+bytes([0x00,0x63])+bytes([0,0,0])
        c.assertEqual(S.lz77_decompress(comp)[:9],b'abcdefabc')
    def test_startup_eng(c):
        g=c.d/'omni'; a=g/'data/arc'; a.mkdir(parents=True); db=b'<?xml version="1.0" encoding="utf8"?>\r\n<mdb>\r\n</mdb>\r\n'
        (a/'startup_eng').write_bytes(S.write_arc({'data/gamedata/musicdb.xml':db}))
        live,src=S.database_file(g); c.assertEqual((live.name,src.name),('startup.arc','startup_eng'))
        c.assertEqual(S.patch_startup(g,[]),0); c.assertTrue((a/'startup.arc').exists()); c.assertFalse((a/'startup_eng.bak').exists())
    def test_hide(c):
        g=c.d/'hide'; a=g/'data/arc'; a.mkdir(parents=True)
        db=b'<?xml version="1.0" encoding="utf8"?>\r\n<mdb>\r\n  <music>\r\n    <mcode __type="u32">5</mcode>\r\n    <basename>keep</basename>\r\n    <title>K</title>\r\n  </music>\r\n  <music>\r\n    <mcode __type="u32">6</mcode>\r\n    <basename>gone</basename>\r\n    <title>G</title>\r\n  </music>\r\n</mdb>\r\n'
        cdb=b'<mdb><course><mcode>1</mcode><music><mcode __type="u32">5</mcode></music></course></mdb>'
        (a/'startup_eng').write_bytes(S.write_arc({'data/gamedata/musicdb.xml':db,'data/gamedata/coursedb.xml':cdb}))
        c.assertEqual(S.course_songs(g),{'5'})
        S.set_hidden(g,hide=['gone'])
        f,d=S.read_arc(a/'startup.arc'); x=S.arc_get(f,d,'data/gamedata/musicdb.xml')
        c.assertNotIn(b'gone',x); c.assertIn(b'keep',x); c.assertEqual(len(ET.fromstring(S.decode_xml(x)[0])),1)
        c.assertEqual(len(S.load_musicdb(g)[0]),2)
        S.set_hidden(g,show=['gone'])
        f,d=S.read_arc(a/'startup.arc'); c.assertEqual(S.arc_get(f,d,'data/gamedata/musicdb.xml'),db)
    def test_delete(c):
        g=c.d/'del'; a=g/'data/arc'; a.mkdir(parents=True); q=g/'data/mdb_apx/ssq'; q.mkdir(parents=True); w=g/'data/sound/win/dance'; w.mkdir(parents=True)
        db=b'<?xml version="1.0" encoding="utf8"?>\n<mdb>\n  <music>\n    <mcode __type="u32">5</mcode>\n    <basename>inco</basename>\n  </music>\n  <music>\n    <mcode __type="u32">6</mcode>\n    <basename>gone</basename>\n  </music>\n</mdb>\n'
        cdb=b'<mdb><music><mcode __type="u32">5</mcode></music></mdb>'
        (a/'startup_eng').write_bytes(S.write_arc({'data/gamedata/musicdb.xml':db,'data/gamedata/coursedb.xml':cdb}))
        for b in ('gone','gone2','inco'): (q/f'{b}.ssq').write_bytes(b'x'); (w/f'{b}.xwb').write_bytes(b'x')
        (q/'gone_1.ssq').write_bytes(b'x')
        with c.assertRaises(S.ConvertError): S.delete_song(g,'inco')
        S.delete_song(g,'gone')
        c.assertFalse((q/'gone.ssq').exists()); c.assertFalse((q/'gone_1.ssq').exists()); c.assertFalse((w/'gone.xwb').exists())
        c.assertTrue((q/'gone2.ssq').exists()); c.assertTrue((q/'inco.ssq').exists())
        c.assertEqual(S.deleted_songs(g),{'gone'})
        f,d=S.read_arc(a/'startup.arc'); c.assertNotIn(b'gone',S.arc_get(f,d,'data/gamedata/musicdb.xml'))
    def test_install(c):
        g=c.d/'game'; (g/'data/arc').mkdir(parents=True); ssq=g/'data/mdb_apx/ssq'; ssq.mkdir(parents=True)
        xml='<?xml version="1.0" encoding="shift-jis"?><mdb><music><mcode __type="u32">100</mcode><basename>abcd</basename><title>T</title><artist>A</artist><bpmmax __type="u16">1</bpmmax><series __type="u8">1</series><limited_ary __type="s32" __count="10">0 0 0 0 -1 -1 0 0 0 -1</limited_ary><diffLv __type="u8" __count="10">1 1 1 1 1 1 1 1 1 1</diffLv><extra_flag __type="u8">3</extra_flag></music></mdb>'.encode()
        name=b'data/gamedata/musicdb.xml\0'; off=0x20+len(name); off+=-off%64
        arc=S.ARC_MAGIC+struct.pack('<III',1,1,2)+struct.pack('<IIII',0x20,off,len(xml),len(xml))+name
        arc+=b'\0'*(off-len(arc))+xml
        (g/'data/arc/startup.arc').write_bytes(arc)
        (g/'data/arc/thumbnail').mkdir()
        (g/'data/arc/thumbnail/jacket_thumbnails_ja_2.arc').write_bytes(S.write_arc({'data/jacket/thumbnail/default_tn.dds':b'x'}))
        (g/'data/arc/thumbnail/jacket_thumbnails_ja_10.arc').write_bytes(S.write_arc({'data/jacket/thumbnail/default_tn.dds':b'y'}))
        s=g/'data/sound/win/dance'; s.mkdir(parents=True)
        (s/'abcd.xsb').write_bytes(AARO_XSB.replace(b'aaro',b'abcd'))
        (s/'abcd.xwb').write_bytes(S.build_xwb('abcd',[(b'',100),(b'',900)],(43,42)))
        (ssq/'abcd.ssq').write_bytes(b'\0'*4)
        before=(g/'data/arc/startup.arc').read_bytes()
        for _ in range(2):
            c.assertEqual(S.main(['install',str(c.d/'songs'),'--game',str(g),'--template-song','abcd']),0)
        c.assertEqual((g/'data/arc/startup.arc').read_bytes(),before)
        out=g/'data_mods/custom_songs'
        r=ET.fromstring(S.decode_xml((out/'gamedata/musicdb.merged.xml').read_bytes())[0])
        c.assertEqual(len(r),1); e=r[0]
        c.assertEqual(e.findtext('title'),'Test Song Mix'); c.assertIsNone(e.find('extra_flag'))
        c.assertEqual(e.findtext('limited_ary'),'-1 -1 0 -1 -1 -1 -1 -1 0 -1'); c.assertEqual(e.findtext('series'),'1')
        c.assertEqual(e.findtext('title_yomi'),'testsongmix'); c.assertEqual(e.findtext('bpmmin'),'120')
        c.assertLess(list(e).index(e.find('bpmmin')),list(e).index(e.find('bpmmax')))
        c.assertEqual(int(e.findtext('mcode')),101); b=e.findtext('basename'); c.assertEqual(len(b),4)
        c.assertTrue((out/f'mdb_apx/ssq/{b}.ssq').exists())
        x=S.read_xwb((out/f'sound/win/dance/{b}.xwb').read_bytes()); c.assertEqual(x['count'],2)
        c.assertEqual(x['name'],b); s=x['sounds'][1]
        c.assertEqual((s['codec'],s['channels'],s['rate'],s['block']),(2,2,44100,48))
        c.assertAlmostEqual(s['samples']/44100,12.5,delta=.1)
        c.assertLess(x['sounds'][0]['samples'],s['samples'])
        f,d=S.read_arc(out/f'arc/jacket/{b}_jk.arc')
        c.assertEqual(list(f),[f'data/jacket/{b}_jk.dds'])
        c.assertEqual(len(S.arc_get(f,d,f'data/jacket/{b}_jk.dds')),128+512*512*4)
    def test_triggers(c):
        ch=c.conv()[1]; t=ch[1]; o=ch[0]['offsets']
        c.assertEqual((t['kind'],t['p2'],t['codes']),(2,1,[0x401,0x102,0x202,0x502,0x302,0x402]))
        c.assertEqual(t['offsets'][-1],o[-1])
        c.assertEqual(t['offsets'][-1]-t['offsets'][-2],4096)
        chart,end=S.trigger_chunk(20480,203776)
        got=S.read_ssq(chart)[0]
        c.assertEqual(got['offsets'],[0,0,12288,16384,208896,212992]); c.assertEqual(end,212992)
    def test_freeze_alignment(c):
        one=S.step_chunk(0x14,1,{0:1,1024:2,2048:4},{3072:1})
        got=S.read_ssq(one+b'\0'*4)[0]
        c.assertEqual(got['steps'],[1,2,4,0]); c.assertEqual(got['extra'],[1,1])
        odd=S.step_chunk(0x14,1,{0:1,1024:2},{3072:1})
        c.assertEqual(S.read_ssq(odd+b'\0'*4)[0]['extra'],[1,1])
        c.assertEqual(struct.unpack_from('<I',odd)[0]%4,0)
    def test_xsb(c):
        x=S.build_xsb(AARO_XSB,'aaro','c2de')
        c.assertEqual(struct.unpack_from('<H',AARO_XSB,8)[0],S.xsb_checksum(AARO_XSB))
        c.assertEqual(struct.unpack_from('<H',x,8)[0],S.xsb_checksum(x))
        c.assertNotIn(b'aaro',x); c.assertEqual(x.count(b'c2de'),4); c.assertIn(b'c2de_s\0',x)
        c.assertEqual(x[0x100:0x120],b'\0'*32)
        c.assertEqual(struct.unpack_from('<H',x,0x124)[0],1); c.assertEqual(struct.unpack_from('<H',x,0x12a)[0],0xffff)
    def test_adpcm(c):
        import numpy as np
        t=np.arange(44100); l=(9000*np.sin(t*.05)).astype('int64'); r=(5000*np.sin(t*.031+1)).astype('int64')
        data,n=S.adpcm_encode(np.stack([l,r],1))
        c.assertEqual(n%128,0); c.assertEqual(len(data)%140,0); c.assertEqual(len(data),n//128*140)
    def test_dxt1(c):
        px=bytes([0,0,255,255])*8+bytes([255,0,0,255])*8
        e=S.dxt1_encode(px,4,4); c0,c1,bits=struct.unpack('<HHI',e)
        c.assertEqual(len(e),8); c.assertGreater(c0,c1)
        c.assertEqual({bits>>i*2&3 for i in range(16)},{0,1})
        c.assertEqual({c0,c1},{0xF800,0x001F})
    def test_compress(c):
        for d in (b'', b'a', b'abcabcabcabc'*50, bytes(range(256))*20):
            c.assertEqual(S.lz77_decompress(S.lz77_compress(d)),d)
        d=b'<music><title>x</title></music>\n'*200
        c.assertLess(len(S.lz77_compress(d)),len(d)/5)
    def test_a3(c):
        g=c.d/'a3'; (g/'data/arc').mkdir(parents=True); (g/'data/mdb/abcd').mkdir(parents=True)
        (g/'data/sound/win/dance').mkdir(parents=True)
        (g/'data/sound/win/dance/abcd.xsb').write_bytes(AARO_XSB.replace(b'aaro',b'abcd'))
        (g/'data/sound/win/dance/abcd.xwb').write_bytes(S.build_xwb('abcd',[(b'',900),(b'',100)],(45,43)))
        (g/'data/mdb/abcd/all.ssq').write_bytes(S.tempo_chunk([0,4096],[0,2000],1000)+b'\0'*4)
        xml='<?xml version="1.0" encoding="shift-jis"?>\n<mdb>\n<music><mcode __type="u32">100</mcode><basename>abcd</basename><title>T</title><artist>A</artist><series __type="u8">19</series></music>\n</mdb>\n'.encode()
        arc=S.write_arc({'data/gamedata/musicdb.xml':xml,'data/other.bin':b'keep'})
        (g/'data/arc/startup.arc').write_bytes(arc)
        a=['install',str(c.d/'songs'),'--game',str(g),'--target','a3','--template-song','abcd']
        c.assertEqual(S.main(a),0); first=(g/'data/arc/startup.arc').read_bytes()
        c.assertEqual(S.main(a),0)
        c.assertEqual((g/'data/arc/startup.arc').read_bytes(),first)
        c.assertEqual((g/'data/arc/startup.arc.bak').read_bytes(),arc)
        f,d=S.read_arc(g/'data/arc/startup.arc')
        c.assertEqual(S.arc_get(f,d,'data/other.bin'),b'keep')
        raw=S.arc_get(f,d,'data/gamedata/musicdb.xml'); c.assertTrue(raw.startswith(b'<?xml'))
        r=ET.fromstring(S.decode_xml(raw)[0]); c.assertEqual(len(r),2)
        e=r[1]; b=e.findtext('basename'); c.assertEqual(e.findtext('series'),'19')
        c.assertIsNone(e.find('limited_ary'))
        c.assertTrue((g/f'data/mdb/{b}/all.ssq').exists())
        c.assertIn(b.encode(),(g/f'data/sound/win/dance/{b}.xsb').read_bytes())
        c.assertEqual(S.read_xwb((g/f'data/sound/win/dance/{b}.xwb').read_bytes())['version'],45)
    def test_warps(c):
        p=c.d/'w.ssc'; p.write_text('#TITLE:x;#BPMS:0=120;#WARPS:4=2;')
        with c.assertRaises(S.ConvertError): S.parse_simfile(p)
    def test_ssc(c):
        p=c.d/'s.ssc'; p.write_text('#TITLE:x;#OFFSET:0;#BPMS:0=120;\n#NOTEDATA:;\n#STEPSTYPE:dance-single;\n#DIFFICULTY:Hard;\n#METER:9;\n#NOTES:\n1000\n0000\n0000\n0000\n;\n')
        s=S.parse_simfile(p); c.assertEqual((s.charts[0].difficulty,s.charts[0].meter),('hard',9))
unittest.main(verbosity=1)
