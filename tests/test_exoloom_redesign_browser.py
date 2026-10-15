"""Isolated touch-browser branding and audited ownership regressions."""
import asyncio
import hashlib
from pathlib import Path
import pytest
import database as db
from tests.test_exoloom_mobile_browser import mobile_browser
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_reference_art_and_lettering_are_exact_and_motion_free(mobile_browser, tmp_path):
    page, *_ = mobile_browser
    await page.evaluate("EXOAppearance.apply({...EXOAppearance.settings(),theme:'exoloom',scene:'reference'})")
    await page.wait_for_selector('#btn-home canvas', state='attached')
    assert 'reference.png' in await page.locator('#blackhole-container').evaluate('(e)=>getComputedStyle(e).backgroundImage')
    assert await page.locator('#exoloom-scene').count() == 0
    assert hashlib.sha256(Path('static/exoloom/reference.png').read_bytes()).hexdigest() == '0594d5d05dc7ad4ec1288de763ac98b7e4667ef242ff738ee453aee00b6ac101'
    assert 'EX umbra Omnia' in await page.locator('#btn-home').inner_text()
    await page.screenshot(path=str(tmp_path/'reference-mobile.png'))
    await page.locator('#btn-folders').tap()
    assert await page.locator('#view-home').evaluate("e=>e.classList.contains('active')")
    # Section labels now use ordinary UI typography; metallic lettering is
    # reserved for the app title and display headings.
    assert await page.locator('.home-section-label canvas').count() == 0
    await page.emulate_media(reduced_motion='reduce')
    await page.evaluate("EXOAppearance.apply({...EXOAppearance.settings(),scene:'animated'})")
    assert await page.locator('#exoloom-scene').count() == 0
    await page.set_viewport_size({'width':1440,'height':900})
    await page.screenshot(path=str(tmp_path/'reference-desktop.png'))
    await page.evaluate('LoomBlackhole.disable()')
    assert await page.locator('#blackhole-container').evaluate('e=>getComputedStyle(e).display') == 'none'
    await page.evaluate('LoomBlackhole.enable()')
    assert 'reference.png' in await page.locator('#blackhole-container').evaluate('e=>getComputedStyle(e).backgroundImage')


async def test_multifile_upload_stays_with_original_branch_and_reserves_slots(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    sibling = await db.add_message(conv['id'],'assistant','Sibling',parent_id=user['id'])
    owner = await page.evaluate('attachmentOwner().key')
    await page.evaluate('''() => {
        window.uploadResolvers=[];
        API.upload = file => new Promise(resolve=>uploadResolvers.push(()=>resolve({path:file.name,url:'/uploads/'+file.name,is_image:false})));
        window.selectionTask=handleImageSelect({target:{files:[new File(['a'],'a.txt'),new File(['b'],'b.txt')],value:''}});
    }''')
    await page.evaluate('id=>switchToBranch(id,id,{exact:true})', sibling['id'])
    assert await page.evaluate('pendingAttachmentUploads()') == 0
    await page.evaluate('uploadResolvers.shift()()')
    await page.wait_for_function('uploadResolvers.length === 1')
    await page.evaluate('uploadResolvers.shift()()')
    await page.evaluate('selectionTask')
    assert await page.evaluate('State.pendingImages.length') == 0
    assert await page.evaluate('key=>JSON.parse(sessionStorage.getItem(key)).pendingImages.map(i=>i.path)', owner) == ['a.txt','b.txt']
    await page.evaluate('''() => {
        State.pendingImages=Array.from({length:4},(_,i)=>({path:String(i),url:'/x',is_image:false}));
        window.one=attachImage(new File(['c'],'c.txt'));
        window.two=attachImage(new File(['d'],'d.txt'));
    }''')
    assert await page.evaluate('uploadResolvers.length') == 1
    await page.evaluate('uploadResolvers.shift()()')
    await page.evaluate('Promise.all([one,two])')
    assert await page.evaluate('State.pendingImages.length') == 5
    button=page.locator('.btn-remove-thumb').first
    assert (await button.bounding_box())['width'] >= 44
    assert (await button.get_attribute('aria-label')).startswith('Remove ')


async def test_delayed_regenerate_does_not_override_navigation(mobile_browser):
    page,conv,user,assistant,_=mobile_browser
    await open_conversation(page,conv,assistant)
    sibling=await db.add_message(conv['id'],'assistant','Newer branch',parent_id=user['id'])
    await page.evaluate('''id => {
        const post=API.post.bind(API); window.launches=[];
        API.post=(path,body)=>path.includes('/regenerate/') ? new Promise(resolve=>window.releaseRegen=resolve) : post(path,body);
        _triggerParallelGenerate=(...args)=>launches.push(args);
        window.regenTask=regenerateMessage(id);
    }''',assistant['id'])
    await page.evaluate('id=>switchToBranch(id,id,{exact:true})',sibling['id'])
    await page.evaluate('id=>releaseRegen({parent_id:id})',user['id'])
    await page.evaluate('regenTask')
    assert await page.evaluate('State.loomCursor.leaf_message_id') == sibling['id']
    assert await page.evaluate('launches.length') == 0


async def test_auth_expiry_stops_reconnect_and_retains_draft(mobile_browser):
    page,conv,user,assistant,_=mobile_browser
    await open_conversation(page,conv,assistant)
    await page.locator('#user-input').fill('Keep this draft')
    await page.evaluate("State.ws.onclose({code:4401,reason:'Authentication expired'})")
    assert await page.evaluate('_wsReconnectTimer') is None
    assert await page.locator('#user-input').input_value() == 'Keep this draft'
