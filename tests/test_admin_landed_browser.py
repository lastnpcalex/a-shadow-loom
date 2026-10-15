"""In-memory browser routes: inbox actions must never write to any server."""
from playwright.async_api import expect

from tests.test_admin_restart_browser import dashboard


def finished(run='gen:77', status='completed'):
    return {'conv_id': 55, 'branch_id': 88, 'draft_msg_id': 88, 'run_id': run,
        'title': 'Fixture workspace', 'project_dir': 'X:/fixture', 'mode': 'goose', 'model': 'fixture-model',
        'kind': 'generation', 'status': status, 'created_at': 1700000000, 'completed_at': 1700000010,
        'loom_thread_id': 'fixture-root', 'root_message_id': 80}


def instance(name='fixture_a', **fields):
    return {'instance': name, 'port': 61011 if name == 'fixture_a' else 61012,
        'scheme': 'http', 'available': True, 'generations': [], 'permissions': [], 'landed': [], **fields}


async def test_history_present_on_first_open_review_survives_reload_and_is_instance_scoped():
    a, b = instance(landed=[finished()]), instance('fixture_b', landed=[finished()])
    state = {'pid': 101, 'supervision': {'instances': [a, b]}}
    async with dashboard(state) as (page, posts, navigations):
        await expect(page.locator('.landed-card')).to_have_count(2)
        chat = page.locator('.landed-card').first.get_by_role('link', name='Chat', exact=True)
        await expect(chat).to_have_attribute('href', 'http://admin.fixture:61011/?conversation=55&view=chat&branch=88')
        await expect(chat).to_have_attribute('target', '_blank')
        # Following a link is not a review action (no handlers that mark read).
        await chat.evaluate('(link) => { link.addEventListener("click", e => e.preventDefault()); link.click(); }')
        await expect(page.locator('.landed-card')).to_have_count(2)
        await page.locator('.landed-card').first.get_by_role('button', name='Mark reviewed', exact=True).click()
        await expect(page.locator('.landed-card')).to_have_count(1)
        await expect(page.locator('.landed-card a').last).to_have_attribute('href', 'http://admin.fixture:61012/?conversation=55&view=chat&branch=88')
        await page.reload()
        await expect(page.locator('.landed-card')).to_have_count(1)
        await page.locator('#landed-show-reviewed').check()
        await expect(page.locator('.landed-card')).to_have_count(2)
        await page.get_by_role('button', name='Mark not reviewed', exact=True).click()
        await page.locator('#landed-show-reviewed').uncheck()
        await expect(page.locator('.landed-card')).to_have_count(2)
        assert not posts


async def test_disappearance_is_not_completion_failures_keep_history_and_notes():
    item = finished()
    permission = {**item, 'request_id': 'note', 'tool_name': 'Bash', 'tool_input': {}}
    owner = instance(generations=[item], permissions=[permission])
    state = {'pid': 101, 'supervision': {'instances': [owner]}}
    async with dashboard(state) as (page, posts, navigations):
        note = page.locator('.permission-card textarea')
        await note.fill('Keep my explanation.')
        owner['generations'] = []
        await page.evaluate('refreshSupervision()')
        await expect(page.locator('.landed-card')).to_have_count(0)
        owner['landed'] = [finished(status='interrupted')]
        await page.evaluate('refreshSupervision()')
        await expect(page.locator('.landed-card')).to_contain_text('Interrupted')
        await expect(note).to_have_value('Keep my explanation.')
        state['supervision_status'] = 503
        await page.evaluate('refreshSupervision()')
        await expect(page.locator('.landed-card')).to_have_class('generation-card landed-card stale')
        await expect(page.locator('#landed-status')).to_contain_text('unavailable')
        state['supervision_status'] = 200
        owner['landed'] = None  # old main/admin code is not an empty inbox
        await page.evaluate('refreshSupervision()')
        await expect(page.locator('#landed-status')).to_contain_text('unsupported')
        await expect(page.locator('.landed-card')).to_have_count(1)
        owner['landed'] = []
        await page.evaluate('refreshSupervision()')
        await expect(page.locator('.landed-card')).to_have_count(0)
        assert not posts


async def test_storage_failure_stays_usable_and_metadata_is_escaped():
    row = finished(status='failed')
    row['title'] = '<img src=x onerror=alert(1)>'
    row['completion_time_estimated'] = True
    state = {'pid': 101, 'supervision': {'instances': [instance(landed=[row])]}}
    async with dashboard(state) as (page, posts, navigations):
        await expect(page.locator('.landed-card')).to_contain_text(row['title'])
        await expect(page.locator('.landed-card img')).to_have_count(0)
        await expect(page.locator('.landed-card')).to_contain_text('Approx. finished')
        await page.evaluate('() => { Storage.prototype.setItem = () => { throw new Error("fixture storage denied"); }; }')
        await page.get_by_role('button', name='Mark reviewed', exact=True).click()
        await expect(page.locator('#landed-status')).to_contain_text('Review storage unavailable')
        await expect(page.locator('.landed-card')).to_have_count(0)
        assert not posts


def turn(number, thread='a', workspace='X:/fixture', conv=55):
    return {**finished(run=f'message:{number}'), 'draft_msg_id': number, 'branch_id': number,
        'completed_at': 1700000000 + number, 'loom_thread_id': thread, 'root_message_id': ord(thread),
        'conv_id': conv, 'project_dir': workspace}


async def test_workspace_roots_coalesce_before_review_filter_and_new_landings_reopen():
    owner = instance(landed=[turn(n) for n in range(100, 105)] + [turn(200, 'b'), turn(300, 'c', 'X:/other')])
    state = {'pid': 101, 'supervision': {'instances': [owner]}}
    async with dashboard(state) as (page, posts, navigations):
        await expect(page.locator('.landed-workspace')).to_have_count(2)
        await expect(page.locator('.landed-card')).to_have_count(3)
        a = page.locator('.landed-card').filter(has_text='Root #97')
        await expect(a.get_by_role('link', name='Chat', exact=True)).to_have_attribute('href', 'http://admin.fixture:61011/?conversation=55&view=chat&branch=104')
        await page.evaluate("window.originalRootCard = [...document.querySelectorAll('.landed-card')].find(c => c.textContent.includes('Root #97'))")
        owner['landed'].append(turn(105))
        await page.evaluate('refreshSupervision()')
        assert await a.evaluate('(card) => card === window.originalRootCard')
        await expect(a.get_by_role('link', name='Chat', exact=True)).to_have_attribute('href', 'http://admin.fixture:61011/?conversation=55&view=chat&branch=105')
        await a.get_by_role('button', name='Mark reviewed', exact=True).click()
        await expect(page.locator('.landed-card')).to_have_count(2)  # No older turns emerge.
        await page.reload()
        await expect(page.locator('.landed-card')).to_have_count(2)
        await page.locator('#landed-show-reviewed').check()
        await expect(page.locator('.landed-card')).to_have_count(3)  # Still one per root, not the old stack.
        await page.locator('#landed-show-reviewed').uncheck()
        owner['landed'].append(turn(106))
        await page.evaluate('refreshSupervision()')
        await expect(page.locator('.landed-card')).to_have_count(3)
        await expect(page.locator('#landed-count')).to_contain_text('3 not reviewed')
        await expect(a.get_by_role('link', name='Chat', exact=True)).to_have_attribute('href', 'http://admin.fixture:61011/?conversation=55&view=chat&branch=106')
        assert not posts


async def test_workspace_grouping_preserves_other_conversations_children_and_unknown_roots():
    child = {**turn(400), 'kind': 'child', 'run_id': 'agent:fixture-child'}
    unknown = {**turn(500), 'loom_thread_id': None, 'root_message_id': None}
    owner = instance(landed=[turn(100), turn(200, 'b', 'x:\\fixture\\'), turn(300, conv=56), child, unknown])
    state = {'pid': 101, 'supervision': {'instances': [owner]}}
    async with dashboard(state) as (page, posts, navigations):
        await expect(page.locator('.landed-workspace')).to_have_count(1)
        await expect(page.locator('.landed-card')).to_have_count(5)
        await expect(page.locator('.landed-card').filter(has_text='agent:fixture-child')).to_contain_text('Agents panel')
        assert not posts
