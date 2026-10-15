"""Provider-free Admin editor preview and save contract."""
from pathlib import Path
import re
import pytest
import visual_settings
from tests.test_agent_activity_browser import activity_browser

async def test_background_studio_previews_orbits_and_saves_explicitly(activity_browser,tmp_path):
    original,*_=activity_browser
    page=await original.context.new_page()
    await page.goto(original.url.split('/')[0]+'//'+original.url.split('/')[2]+'/api/visual-settings')
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    await page.route('**/assets/**',lambda route:route.fulfill(path=str(Path('static/admin')/route.request.url.rsplit('/',1)[-1].split('?')[0])))
    saved=[]
    async def profile(route):
        if route.request.method=='POST':
            value=visual_settings.validate(route.request.post_data_json);saved.append(value)
            await route.fulfill(json={'values':value})
        else:
            await route.fulfill(json={'values':visual_settings.load(),'fields':visual_settings.schema(),'main_port':int(page.url.split(':')[2].split('/')[0])})
    await page.route('**/api/visual-settings',profile)
    html=Path('static/admin/index.html').read_text(encoding='utf-8')
    html=re.sub(r'<script[^>]*>[\s\S]*?</script>','',html)
    await page.set_content(html)
    await page.add_script_tag(path='static/admin/admin-visual.js')
    await page.evaluate("document.querySelector('#view-settings').classList.add('active')")
    await page.locator('#visual-load').click()
    await page.locator('[name="observer.motion"]').check()
    await page.locator('[name="observer.orbit"]').select_option('eccentric')
    await page.locator('[name="observer.apoapsis"]').fill('40')
    assert not saved
    await page.locator('#visual-form [type=submit]').click()
    await page.wait_for_function("document.querySelector('#visual-status').textContent.startsWith('Saved.')")
    assert saved[0]['observer.motion'] is True
    assert saved[0]['observer.orbit']=='eccentric'
    assert saved[0]['observer.apoapsis']==40
    frame=page.frame_locator('#visual-preview')
    await frame.locator('#scene canvas').wait_for(state='attached',timeout=60000)
    await frame.locator('body').evaluate('''async () => {
        for (let i=0;i<120 && !window.NeonViewer;i++) await new Promise(resolve=>setTimeout(resolve,250));
        if(!window.NeonViewer) throw new Error('Preview renderer did not initialize');
        await NeonViewer.ready;
    }''')
    assert await frame.locator('body').evaluate('NeonViewer.params.observer.orbit')=='eccentric'
    assert await frame.locator('body').evaluate('NeonViewer.params.observer.apoapsis')==40
    await page.locator('#visual-load').scroll_into_view_if_needed()
    await page.screenshot(path=str(tmp_path/'background-studio.png'))
    assert not errors,errors
    await page.close()
