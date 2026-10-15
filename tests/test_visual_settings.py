"""Shared background validation and private Admin write boundary."""
import httpx
import pytest
import visual_settings as visual

def test_profile_validates_and_persists_outside_checkout(monkeypatch,tmp_path):
    monkeypatch.setenv('EXO_STATE_DIR',str(tmp_path))
    settings=visual.load(); settings['observer.motion']=True; settings['observer.orbit']='eccentric'
    settings['look.nebula_color']='#123abc'
    assert visual.save(settings)==settings
    assert visual.load()==settings
    assert (tmp_path/'visual-settings.json').exists()
    for bad in [{'observer.distance':float('nan')},{'observer.distance':True},{'observer.orbit':'bad'},{'__proto__':{}},{'look.floor_color':'javascript:bad'},{'observer.periapsis':30,'observer.apoapsis':4}]:
        with pytest.raises(ValueError): visual.save(bad)
    assert visual.load()==settings

async def test_admin_profile_routes_validate_and_main_is_read_only(monkeypatch,tmp_path):
    import admin_server,server
    from fastapi import FastAPI
    monkeypatch.setenv('EXO_STATE_DIR',str(tmp_path))
    app=FastAPI()
    app.add_api_route('/admin',admin_server.api_visual_settings,methods=['GET'])
    app.add_api_route('/admin',admin_server.api_save_visual_settings,methods=['POST'])
    app.add_api_route('/main',server.api_visual_settings,methods=['GET'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
        data=(await client.get('/admin')).json()
        assert len(data['fields'])>=25
        data['values']['observer.motion']=True
        assert (await client.post('/admin',json=data['values'])).status_code==200
        assert (await client.get('/main')).json()['observer.motion'] is True
        assert (await client.post('/main',json={})).status_code==405
        assert (await client.post('/admin',json={'observer.distance':0})).status_code==400
