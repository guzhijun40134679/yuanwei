"""小修36：真实选料/分页/跨区链，全部合成消息，不读取评测材料。"""
import dataclasses
import json
import re

import pytest

from helpers import Clock, Scripted
from helpers2 import IDENT
from yuanwei import Config, Memory, hosts as H, relevance as RL, vectors as VX, paging as PG
from yuanwei.errors import BadCursor, HistoryDeleted
from yuanwei.ranked_pages import GroupTooLarge
from yuanwei.access import Access
from yuanwei.idsource import InProcessIdentity
from yuanwei.tools import ToolSurface


def seeded(tmp_path, *, text=None, threads=False, **config):
    mem = Memory.open(str(tmp_path/'m.sqlite'), 'w', config=Config(**config), provider=Scripted('thread' if threads else 'skip'), clock=Clock())
    texts = text or ['needle '+('old words '*20)+str(i) for i in range(6)] + ['needle needle 最新目标']
    ids = [mem.record('h','S',str(i),'user', t, time='2027-01-%02d'%(i+1))['msg_id'] for i,t in enumerate(texts)]
    mem.end_session('h','S'); mem.run()
    return mem, ids


def native_pages(mem, query):
    out=[]; page=mem.recall(query)
    while True:
        out.append(page)
        if not page['cursor']: return out
        page=mem.recall(cursor=page['cursor'])


def surface(mem):
    access=Access(mem); identities=InProcessIdentity(); identities.set('pi','caller','','T');access.begin_turn('pi','caller','T')
    return ToolSurface(mem,access,H.profile('pi'),identities)


def token(text):
    match=re.search(r'只给 cursor=(\S+)', text)
    return match.group(1) if match else None


def cross_world(tmp_path, **cfg):
    from test_hermes_workspace import World, turn
    texts={key:['needle '+key+' '+('padding '*15)+str(i) for i in range(4)] for key in ('life','proj:p-a','proj:p-b')}
    world=World(tmp_path,scope='all_open',texts=texts,mem_cfg={'neighbors':0,'recall_page_chars':950,**cfg})
    adapter=world.adapter('/elsewhere');turn(adapter,1,'a new question')
    return world,adapter


def test_latest_high_rank_is_first_page_while_selected_set_and_page_time_order_stay(tmp_path):
    mem,ids=seeded(tmp_path,neighbors=0,recall_page_chars=620)
    selected=mem._reader.select('needle')['selected']
    pages=native_pages(mem,'needle')
    assert len(pages)>1 and ids[-1] in [d['msg_id'] for d in pages[0]['delivered']]
    assert '页内按时间' in pages[0]['text']
    delivered=[d['msg_id'] for page in pages for d in page['delivered']]
    assert len(delivered)==len(set(delivered)) and set(delivered)=={r['msg_id'] for r in selected}
    for page in pages:
        seq=[mem.store.message_row('w',d['msg_id'])['seq'] for d in page['delivered']]
        assert seq==sorted(seq)
    mem.cfg.recall_ranked_pages=False
    old=native_pages(mem,'needle')
    assert ids[-1] not in [d['msg_id'] for d in old[0]['delivered']]
    assert {d['msg_id'] for p in old for d in p['delivered']}==set(delivered)


def test_first_parent_edges_connect_direct_hits_and_their_neighbors(tmp_path):
    mem,ids=seeded(tmp_path,text=['a','needle one','needle two','b','c'],neighbors=1)
    selection=mem._reader.select('needle');pack=selection['pack'];groups=pack['page_groups']
    assert sum(len(g) for g in groups)==len(set(m for g in groups for m in g))==len(selection['selected'])
    owners={m:i for i,g in enumerate(groups) for m in g}
    assert owners[ids[0]] == owners[ids[1]] == owners[ids[2]] == owners[ids[3]]
    assert set(m for g in groups for m in g)==set(pack['最终']['选中'])


def test_occasion_and_neighbors_are_never_split_and_oversize_is_explicit(tmp_path):
    mem,ids=seeded(tmp_path,text=['old '+('x '*100),'middle '+('y '*100),'needle latest'],threads=True,neighbors=1)
    selected=mem._reader.select('needle')
    assert selected['pack']['page_groups']==[selected['pack']['最终']['选中']]
    assert len(native_pages(mem,'needle'))==1
    mem.cfg.recall_page_chars=300
    with pytest.raises(GroupTooLarge,match='get_messages'):
        mem.recall('needle')
    result=surface(mem).call('recall',{'query':'needle'})
    assert result.status=='error' and result.reason=='group_too_large' and 'get_messages' in result.text


def test_same_cursor_same_page_and_configuration_change_rejected(tmp_path):
    mem,_=seeded(tmp_path,neighbors=0,recall_page_chars=620)
    first=mem.recall('needle');cur=first['cursor'];assert cur
    a=mem.recall(cursor=cur);b=mem.recall(cursor=cur)
    assert a['text']==b['text'] and a['delivered']==b['delivered']
    mem.cfg.recall_ranked_pages=False
    with pytest.raises(BadCursor,match='重新查'): mem.recall(cursor=cur)


@pytest.mark.parametrize('ranked',[False,True])
def test_old_cursor_without_page_version_is_rejected(tmp_path,ranked):
    mem,_=seeded(tmp_path,neighbors=0,recall_page_chars=620,recall_ranked_pages=ranked)
    first=mem.recall('needle');cur=first['cursor'];cid=cur.split(':')[0]
    data=mem.store.get_cursor(cid)['data'];data.pop('paging_version');mem.store.update_cursor(cid,data)
    with pytest.raises(BadCursor,match='重新查'):mem.recall(cursor=cur)


def test_legacy_cursor_keeps_explicit_first_limit_when_continuation_omits_it(tmp_path):
    mem,_=seeded(tmp_path,neighbors=0,recall_ranked_pages=False)
    first=mem.recall('needle',limit_chars=200);cur=first['cursor'];assert cur
    one=mem.recall(cursor=cur);two=mem.recall(cursor=cur,limit_chars=200)
    assert one['text']==two['text'] and one['delivered']==two['delivered']
    with pytest.raises(BadCursor,match='重新查'):mem.recall(cursor=cur,limit_chars=201)


def test_tool_multiquery_page_preserves_top_hits_and_cursor_replay(tmp_path):
    mem,ids=seeded(tmp_path,neighbors=0,recall_page_chars=780)
    sf=surface(mem);first=sf.call('recall',{'query':'needle','keywords':['最新目标']})
    assert first.status=='ok' and ids[-1] in first.text and first.meta['cursor']
    again=sf.call('recall',{'cursor':first.meta['cursor']});same=sf.call('recall',{'cursor':first.meta['cursor']})
    assert again.status=='ok' and again.text==same.text


def test_empty_tool_result_still_checks_host_limit_and_keeps_keyword_notice(tmp_path):
    mem,_=seeded(tmp_path)
    sf=surface(mem)
    normal=sf.call('recall',{'query':'spektralab','keywords':['alpha','beta','gamma','delta']})
    assert normal.status=='ok' and '没有用的是' in normal.text
    tiny=H.Path('mcp_return',(H.Limit('cp',2,'reject'),),{})
    sf.profile=dataclasses.replace(sf.profile,paths=dict(sf.profile.paths,mcp_return=tiny))
    result=sf.call('recall',{'query':'spektralab'})
    assert result.status=='error' and result.reason=='return_too_large'


def test_note_is_unchanged_by_page_mode(tmp_path):
    mem,_=seeded(tmp_path,neighbors=0)
    before=mem.note('needle');mem.cfg.recall_ranked_pages=False;after=mem.note('needle')
    assert before['note']==after['note'] and before['delivered']==after['delivered']


def test_cross_pages_round_robin_sources_and_log_only_actual_page(tmp_path):
    from test_hermes_workspace import call
    world,a=cross_world(tmp_path)
    result=call(a,'recall',{'query':'needle'})
    assert result['status']=='ok',result
    cur=token(result['text']);assert cur and cur.startswith('M')
    db=world.mem('life').store
    agg=db.get_cursor(cur[1:].split(':')[0])['data']
    assert 'chunks' not in agg and 'needle' not in json.dumps(agg) and 'fingerprint' not in json.dumps(agg)
    flat=[x for p in agg['pages'] for x in p]
    assert [s for s,i in flat[:3]]==[0,1,2]
    first_ids=set(re.findall(r'｜(m-[a-f0-9]+)〕',result['text']))
    logged=set()
    for ns,raw in db.conn.execute("SELECT ns,detail FROM read_log WHERE kind='assembled'"):
        spans=json.loads(raw)['spans'];logged|={s['msg_id'] for s in spans}
        assert all(world.mem(ns).store.message_row(ns,s['msg_id']) is not None for s in spans)
    assert logged==first_ids
    assert result['text'].count('截点')==result['text'].count('〔工作区：')
    again=call(a,'recall',{'cursor':cur});same=call(a,'recall',{'cursor':cur})
    assert again['status']=='ok' and again['text']==same['text']


def test_cross_scope_change_permission_revoke_and_foreign_delete_block_continuation(tmp_path):
    from test_hermes_workspace import call
    from yuanwei.deletion import DeletionService,legacy_conv_id
    world,a=cross_world(tmp_path)
    foreign=world.mem('proj:p-a');foreign.record('hermes','foreign-delete','fd','user','unrelated extra')
    first=call(a,'recall',{'query':'needle'});cur=token(first['text']);assert cur
    a.scope='isolated';changed=call(a,'recall',{'cursor':cur})
    assert changed['status']=='error' and changed['reason']=='bad_cursor'
    a.scope='all_open';foreign.set_read_permission(False)
    denied=call(a,'recall',{'cursor':cur});assert denied['status']=='refused'
    foreign.clear_read_permission();service=DeletionService(foreign.db_path,config_dir=foreign.config_dir,clock=foreign.clock)
    try: assert service.delete_conversation(legacy_conv_id('hermes','foreign-delete','')).status=='complete'
    finally:service.close()
    owner=world.mem('life').store.get_cursor(cur[1:].split(':')[0]);assert owner['data'].get('sources')
    gone=call(a,'recall',{'cursor':cur})
    assert gone['status']=='error' and gone['reason']=='history_deleted'


def test_partial_finish_failure_marks_prior_source_as_not_delivered(tmp_path,monkeypatch):
    from test_hermes_workspace import call
    world,a=cross_world(tmp_path,recall_page_chars=30000)
    original=Access.finish_read
    def fail(self,tk,text,*args,**kwargs):
        if self.ns=='proj:p-a':return {'status':'error','reason':'test_failure','text':'blocked'}
        return original(self,tk,text,*args,**kwargs)
    monkeypatch.setattr(Access,'finish_read',fail)
    result=call(a,'recall',{'query':'needle'})
    assert result['status']=='error'
    logs=[(ns,kind,json.loads(raw)) for ns,kind,raw in world.mem('life').store.conn.execute('SELECT ns,kind,detail FROM read_log')]
    assert any(ns=='life' and kind=='injection_failed' for ns,kind,data in logs)
    assert not any(ns=='proj:p-b' and kind=='returned' for ns,kind,data in logs)


def test_empty_cross_result_rechecks_every_source_permission(tmp_path,monkeypatch):
    from test_hermes_workspace import call
    world,a=cross_world(tmp_path)
    foreign=world.mem('proj:p-b');original=foreign._reader.select_multi
    def deny_after(*args,**kwargs):
        out=original(*args,**kwargs);foreign.set_read_permission(False);return out
    monkeypatch.setattr(foreign._reader,'select_multi',deny_after)
    result=call(a,'recall',{'query':'spektralab'})
    assert result['status']=='refused'


def test_only_foreign_hits_are_known_to_owner_close_receipt_without_foreign_content_hash(tmp_path):
    from test_hermes_workspace import call
    world,a=cross_world(tmp_path,recall_page_chars=30000)
    result=call(a,'recall',{'query':'proj'})
    assert result['status']=='ok' and '〔工作区：生活〕' not in result['text']
    state=a.acc._reads_state(a.acc.current_turn('hermes',a.session,''))
    assert state in ('confirmed','unconfirmed') and state!='none'
    summaries=[json.loads(r[0]) for r in a.mem.store.conn.execute("SELECT detail FROM read_log WHERE ns='life' AND kind='returned'")]
    summary=next(d for d in summaries if d.get('cross_workspace'))
    assert set(summary)=={'delivery','cross_workspace','source_count'}


@pytest.mark.parametrize('ranked',[False,True])
@pytest.mark.parametrize('different',[False,True])
def test_real_adapter_query_scope_once_per_full_identity(tmp_path,monkeypatch,ranked,different):
    from test_hermes_workspace import call
    from helpers2 import FakeEmbedClient
    world,a=cross_world(tmp_path,recall_ranked_pages=ranked,recall_page_chars=30000)
    client=FakeEmbedClient();registry={}
    for i,mem in enumerate(world.mems.values()):
        ident=dict(IDENT,runtime_version=('other' if different and i==2 else IDENT['runtime_version']))
        fp=VX.identity_fp(ident);key=RL.model_key(ident)
        registry.setdefault(key,{'threshold':-1.0,'vector_fps':[],'evidence':'synthetic-adapter-query-scope'})['vector_fps'].append(fp)
        mem.cfg.retrieval='hybrid';mem.cfg.vector_fallback='partial';mem.vector_fp=fp;mem.store.put_identity(fp,ident)
        mem._reader.vector_fp=fp;mem._reader.embed_client=client;mem._reader._snaps.clear()
        snap=mem._reader.snapshot();VX.register(mem.store,ident,{an.text:[1,0.1,0.2,0.3] for an in snap.anchors})
    monkeypatch.setattr(RL,'CALIBRATIONS',registry)
    result=call(a,'recall',{'query':'needle question'})
    assert result['status']=='ok',result
    assert len(client.calls)==(2 if different else 1)


def test_interleaved_outer_permit_rejection_marks_only_its_own_source_tickets(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_hermes_workspace import call
    world,a=cross_world(tmp_path,recall_page_chars=30000)
    verify=a.win.verify_before_return;state={'nested':False}
    def returned():
        return {r[0] for r in a.mem.store.conn.execute("SELECT DISTINCT call_id FROM read_log WHERE kind='returned'")}
    def interleave(secret):
        if state['nested']: return verify(secret)
        state['nested']=True
        state['outer']=returned()
        inner=call(a,'recall',{'query':'needle'})
        assert inner['status']=='ok'
        state['inner']=returned()-state['outer']
        return SimpleNamespace(deliver=False,reason='window_closed')
    monkeypatch.setattr(a.win,'verify_before_return',interleave)
    outer=call(a,'recall',{'query':'needle'})
    assert outer['status']=='refused' and state['inner'] and state['outer']
    failed={r[0] for r in a.mem.store.conn.execute("SELECT DISTINCT call_id FROM read_log WHERE kind='injection_failed'")}
    assert failed==state['outer'] and not failed & state['inner']

@pytest.mark.parametrize('ranked', [True, False])
@pytest.mark.parametrize('cross', [True, False])
def test_dropped_keyword_redacted_before_clipping_and_persistence(tmp_path, ranked, cross):
    secret = 'sk-ant-' + 'syntheticCredentialOnly' * 3
    args = {'query': 'needle', 'keywords': ['one', 'two', 'three', secret]}
    if cross:
        world, adapter = cross_world(tmp_path, recall_ranked_pages=ranked)
        from test_hermes_workspace import call
        result = call(adapter, 'recall', args)
        text = result['text']
        memories = list(world.mems.values())
    else:
        mem, _ = seeded(tmp_path, neighbors=0, recall_page_chars=620, recall_ranked_pages=ranked)
        result = surface(mem).call('recall', args)
        text = result.text
        memories = [mem]
    assert '密钥已挡掉' in text
    assert secret[:24] not in text and secret not in text
    for mem in memories:
        for (raw,) in mem.store.conn.execute('SELECT data FROM cursors'):
            assert secret[:24] not in raw and secret not in raw


@pytest.mark.parametrize("cross", [False, True])
def test_whole_page_spans_use_encoded_content_in_single_and_cross(tmp_path, cross):
    if cross:
        from test_hermes_workspace import World, turn, call
        world = World(tmp_path, scope="all_open", texts={k: ["needle\nsecond line"] for k in ("life", "proj:p-a", "proj:p-b")}, mem_cfg={"neighbors": 0})
        adapter = world.adapter("/elsewhere"); turn(adapter, 1, "question")
        result = call(adapter, "recall", {"query": "needle"})
        assert result["status"] == "ok"
        found = 0
        for mem in world.mems.values():
            for (raw,) in mem.store.conn.execute("SELECT detail FROM read_log WHERE ns=? AND kind='assembled'", (mem.ns,)):
                for span in json.loads(raw).get("spans", []):
                    content = mem.store.message_row(mem.ns, span["msg_id"])["content"]
                    assert span["start"] == 0 and span["end"] == len(mem._reader.mp.encode(content))
                    found += 1
        assert found
    else:
        mem, ids = seeded(tmp_path, text=["needle\nsecond line"], neighbors=0)
        page = mem.recall("needle")
        content = mem.store.message_row(mem.ns, ids[0])["content"]
        assert page["delivered"] == [{"msg_id": ids[0], "start": 0, "end": len(mem._reader.mp.encode(content))}]
        assert "␊" in page["text"] and "\n" in content
