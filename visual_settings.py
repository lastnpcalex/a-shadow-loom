"""Non-secret shared background preset; only authenticated Admin can write."""
import json
import math
import os
import threading
from exo_state import state_path

# path, label, default, min, max (choices or booleans use None bounds)
FIELDS = [
    ('observer.motion','Automatic orbit',False,None,None),
    ('observer.orbit','Orbit shape','circular',['circular','eccentric'],None),
    ('observer.distance','Circular radius',30,3.01,60),
    ('observer.periapsis','Closest orbit radius',3.2,3.2,60),
    ('observer.apoapsis','Farthest orbit radius',22.6,3.2,100),
    ('observer.orbital_inclination','Orbit inclination',4.27,-90,90),
    ('observer.azimuth','View azimuth',-90,-180,180),
    ('observer.elevation','View elevation',0,-85,85),
    ('observer.rotation_speed','View rotation speed',0,-30,30),
    ('time_scale','Animation speed',3.18,0,8),
    ('camera.height','Camera height',-5.4,-100,100),
    ('camera.pitch','Camera pitch',6.4663,-90,90),
    ('camera.yaw','Camera yaw',-.2399,-180,180),
    ('look.disk_tilt','Disk tilt',17,-90,90),
    ('look.disk_yaw','Disk heading',90,-180,180),
    ('look.disk_outer','Disk outer radius',13,4,60),
    ('look.disk_speed','Disk rotation',4.06,0,10),
    ('look.nebula_amount','Nebula strength',1.7,0,3),
    ('look.nebula_scale','Nebula scale',8,.1,20),
    ('look.nebula_color','Nebula color','#ff00ff',None,None),
    ('look.hole_glow_color','Black-hole glow','#00ffff',None,None),
    ('look.floor_color','Floor color','#00ffff',None,None),
    ('look.floor_major_color','Major grid color','#00ffff',None,None),
    ('neon_floor','Floor grid',True,None,None),
    ('neon_grid','Disk grid',False,None,None),
    ('look.floor_cell','Floor cell size',2.5,.5,20),
    ('look.floor_reflection','Floor reflection',.45,0,1),
    ('look.floor_height','Floor height',6,-50,100),
    ('look.exposure','Exposure',1.381,.1,5),
    ('look.bloom_strength','Bloom',.693,0,3),
    ('look.render_scale','Maximum render resolution',1,.25,1),
    ('look.auto_res','Adapt resolution to frame budget',True,None,None),
]
_lock = threading.Lock()

def validate(values):
    if not isinstance(values,dict) or set(values) - {f[0] for f in FIELDS}:
        raise ValueError('Unknown background setting')
    result = {}
    for key,label,default,low,high in FIELDS:
        value = values.get(key,default)
        if isinstance(default,bool): valid = isinstance(value,bool)
        elif isinstance(default,str):
            valid = value in low if isinstance(low,list) else isinstance(value,str) and len(value)==7 and value.startswith('#') and all(c in '0123456789abcdefABCDEF' for c in value[1:])
        else: valid = not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) and low <= value <= high
        if not valid: raise ValueError(f'{label}: invalid value')
        result[key] = value
    if result['observer.apoapsis'] < result['observer.periapsis']:
        raise ValueError('Farthest orbit radius must be at least the closest radius')
    return result

def load():
    try: return validate(json.loads(state_path('visual-settings.json').read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError): return validate({})

def save(values):
    value = validate(values)
    with _lock:
        path = state_path('visual-settings.json'); path.parent.mkdir(parents=True,exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
        os.replace(temporary,path)
    return value

def schema():
    return [dict(path=k,label=l,default=d,min=lo,max=hi) for k,l,d,lo,hi in FIELDS]
