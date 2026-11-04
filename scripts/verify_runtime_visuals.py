"""Check PBR and authored corner normals in default trimesh/yourdfpy loads.

This validates consumer scene data, not renderer-specific lighting or screenshots.
Only static, untextured robot visual GLBs are in the current validation profile.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
import trimesh
import yourdfpy
from verify_normal_updates import independent_read_glb, independent_accessor

ROOT = Path(__file__).resolve().parents[1]
COLOR_TOLERANCE = .5 / 255 + 1e-12
NORMAL_TOLERANCE = 1e-6
POSITION_TOLERANCE = 1e-10
FRAME_TOLERANCE = 1e-9
DTYPES = {'b':'i1','B':'u1','h':'<i2','H':'<u2','I':'<u4','f':'<f4'}


def accessor(tree, binary, index):
    rows, code, width = independent_accessor(tree, binary, index)
    return np.frombuffer(b''.join(rows), dtype=DTYPES[code]).reshape(-1, width)


def node_matrix(node):
    if 'matrix' in node:
        return np.asarray(node['matrix'], dtype=float).reshape(4, 4).T
    x,y,z,w = np.asarray(node.get('rotation', [0,0,0,1]), dtype=float)
    length = math.sqrt(x*x+y*y+z*z+w*w)
    x,y,z,w = x/length,y/length,z/length,w/length
    rotation = np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                         [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                         [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    result=np.eye(4);result[:3,:3]=rotation @ np.diag(node.get('scale',[1,1,1]))
    result[:3,3]=node.get('translation',[0,0,0])
    return result


def urdf_origin(element):
    if element is None:return np.eye(4)
    roll,pitch,yaw=[float(x) for x in element.get('rpy','0 0 0').split()]
    cr,sr,cp,sp,cy,sy=math.cos(roll),math.sin(roll),math.cos(pitch),math.sin(pitch),math.cos(yaw),math.sin(yaw)
    result=np.eye(4)
    result[:3,:3]=[[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr],
                   [sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr],[-sp,cp*sr,cp*cr]]
    result[:3,3]=[float(x) for x in element.get('xyz','0 0 0').split()]
    return result


def material_key(spec):
    return (tuple(spec['color_quantized']),round(spec['metallic'],12),round(spec['roughness'],12),
            bool(spec['double_sided']),spec['alpha_mode'],round(spec['alpha_cutoff'],12),
            tuple(round(x,12) for x in spec['emissive']))


def raw_material(tree, primitive):
    if 'material' not in primitive:
        raise ValueError('A referenced visual primitive has no embedded material')
    material=tree['materials'][primitive['material']]
    if material.get('extensions'):
        raise ValueError('Material extensions require extension-aware runtime validation')
    pbr=material.get('pbrMetallicRoughness',{})
    if any('Texture' in key for key in material) or any('Texture' in key for key in pbr):
        raise ValueError('Textured material requires texture-aware runtime validation')
    color=np.asarray(pbr.get('baseColorFactor',[1,1,1,1]),dtype=float)
    return {'color':color.tolist(),'color_quantized':np.rint(np.clip(color,0,1)*255).astype(int).tolist(),
            'metallic':float(pbr.get('metallicFactor',1)), 'roughness':float(pbr.get('roughnessFactor',1)),
            'double_sided':material.get('doubleSided',False),'alpha_mode':material.get('alphaMode','OPAQUE'),
            'alpha_cutoff':float(material.get('alphaCutoff',.5)),'emissive':material.get('emissiveFactor',[0,0,0])}


def loaded_material(mesh):
    if not isinstance(mesh.visual,trimesh.visual.texture.TextureVisuals):
        raise AssertionError(f'Embedded PBR TextureVisuals lost: {type(mesh.visual).__name__}')
    material=mesh.visual.material
    if not isinstance(material,trimesh.visual.material.PBRMaterial):
        raise AssertionError(f'PBR material converted to {type(material).__name__}')
    rgba=material.baseColorFactor
    if rgba is None:raise AssertionError('Embedded baseColorFactor was dropped')
    values=np.asarray(rgba)
    color=values.astype(float)/255 if values.dtype.kind in 'iu' else values.astype(float)
    metallic=material.metallicFactor
    roughness=material.roughnessFactor
    emissive=material.emissiveFactor
    cutoff=material.alphaCutoff
    return {'color':color.tolist(),'color_quantized':np.rint(np.clip(color,0,1)*255).astype(int).tolist(),
            'metallic':1. if metallic is None else float(metallic),'roughness':1. if roughness is None else float(roughness),
            'double_sided':bool(material.doubleSided),'alpha_mode':material.alphaMode or 'OPAQUE',
            'alpha_cutoff':.5 if cutoff is None else float(cutoff),'emissive':[0,0,0] if emissive is None else np.asarray(emissive).tolist()}


@lru_cache(maxsize=16)
def _raw_groups(path,mtime,size):
    payload=Path(path).read_bytes();tree,binary=independent_read_glb(payload)
    if any(tree.get(key) for key in ('skins','animations','images','textures','extensionsRequired','extensionsUsed','extensions')):
        raise ValueError('Runtime profile expects static untextured GLB geometry')
    groups=[];nodes=tree['nodes'];scene=tree['scenes'][tree.get('scene',0)]
    stack=[(i,np.eye(4),frozenset()) for i in reversed(scene.get('nodes',[]))]
    while stack:
        index,parent,ancestors=stack.pop()
        if index in ancestors:raise ValueError('Cyclic GLB nodes')
        node=nodes[index];frame=parent@node_matrix(node)
        if node.get('extensions'):
            raise ValueError('Node extensions require extension-aware runtime validation')
        stack.extend((child,frame,ancestors|{index}) for child in reversed(node.get('children',[])))
        if 'mesh' not in node:continue
        mesh=tree['meshes'][node['mesh']]
        if mesh.get('extensions'):
            raise ValueError('Mesh extensions require extension-aware runtime validation')
        for pi,primitive in enumerate(mesh['primitives']):
            if primitive.get('extensions'):
                raise ValueError('Primitive extensions require extension-aware runtime validation')
            if primitive.get('mode',4)!=4:continue
            attrs=primitive['attributes']
            if set(attrs)!={'POSITION','NORMAL'}:raise ValueError('Runtime profile requires authored POSITION/NORMAL-only triangle attributes')
            vertices=accessor(tree,binary,attrs['POSITION']).astype(float)
            faces=accessor(tree,binary,primitive['indices']).reshape(-1,3)
            normals=accessor(tree,binary,attrs['NORMAL']).astype(float)
            groups.append({'positions':vertices[faces],'normals':normals[faces],'frame':frame,
                           'material':raw_material(tree,primitive),'source_primitive':[node['mesh'],pi]})
    return groups,hashlib.sha256(payload).hexdigest()


def raw_groups(path):
    path=Path(path).resolve();stat=path.stat()
    return _raw_groups(str(path),stat.st_mtime_ns,stat.st_size)


def loaded_groups(scene,relative_to=None):
    groups=[]
    for node in scene.graph.nodes_geometry:
        frame,name=scene.graph.get(node,frame_from=relative_to)
        mesh=scene.geometry[name]
        if not isinstance(mesh,trimesh.Trimesh):continue
        vertices=np.asarray(mesh.vertices,dtype=float);faces=np.asarray(mesh.faces,dtype=np.int64)
        had_authored_normals='vertex_normals' in mesh._cache.cache
        if not had_authored_normals:raise AssertionError(f'Authored NORMAL array is absent from the default-loaded cache: {node}')
        normals=np.asarray(mesh.vertex_normals,dtype=float)
        if normals.shape!=vertices.shape:raise AssertionError('Loaded normal/position attribute shape differs')
        groups.append({'positions':vertices[faces],'normals':normals[faces],'frame':frame,
                       'material':loaded_material(mesh),'node':node,'geometry':name,'mesh':mesh})
    return groups


def canonical_rows(groups):
    positions=np.concatenate([group['positions'] for group in groups]).astype(float,copy=False)
    normals=np.concatenate([group['normals'] for group in groups]).astype(float,copy=False)
    lengths=np.linalg.norm(normals,axis=2,keepdims=True)
    if not np.isfinite(positions).all() or not np.isfinite(normals).all() or (lengths<=1e-12).any():
        raise AssertionError('Non-finite geometry or zero runtime normal')
    normals=normals/lengths
    # Canonical cyclic rotation preserves winding and associates each corner N
    # with its P. Reindexing vertices/faces and duplicate instances remain valid.
    first=np.lexsort((positions[:,:,2],positions[:,:,1],positions[:,:,0]),axis=1)[:,0]
    minimum=positions[np.arange(len(positions)),first]
    tied=np.flatnonzero(np.sum(np.all(positions==minimum[:,None,:],axis=2),axis=1)>1)
    for face in tied:
        # A zero-area triangle can repeat its smallest position. Compare full
        # cyclic corner tuples so a legal face rotation does not pick a
        # different authored normal or reverse the corner order.
        candidates=[]
        for start in range(3):
            order=(start+np.arange(3))%3
            candidates.append(np.r_[positions[face,order].ravel(),normals[face,order].ravel()])
        candidates=np.asarray(candidates)
        first[face]=np.lexsort(tuple(candidates[:,i] for i in range(17,-1,-1)))[0]
    indices=(first[:,None]+np.arange(3))%3
    row=np.arange(len(positions))[:,None]
    positions=positions[row,indices];normals=normals[row,indices]
    result=np.column_stack((positions.reshape(-1,9),normals.reshape(-1,9)))
    result[result==0]=0.0
    return result[np.lexsort(tuple(result[:,i] for i in range(17,-1,-1)))]


def compare_groups(expected,actual):
    frames=[];eb=defaultdict(list);ab=defaultdict(list);frame_error=0.
    for group in expected:
        frame=group['frame']
        matches=[i for i,value in enumerate(frames) if np.allclose(frame,value,atol=FRAME_TOLERANCE,rtol=0)]
        index=matches[0] if matches else len(frames)
        if not matches:frames.append(frame)
        eb[index,material_key(group['material'])].append(group)
    for group in actual:
        errors=[float(np.max(np.abs(group['frame']-frame))) for frame in frames]
        if not errors or min(errors)>FRAME_TOLERANCE:raise AssertionError('Loaded geometry instance transform has no source counterpart')
        index=int(np.argmin(errors));frame_error=max(frame_error,errors[index])
        ab[index,material_key(group['material'])].append(group)
    if eb.keys()!=ab.keys():raise AssertionError('PBR/material or transformed instance partitions differ from the source')
    max_p=max_n=max_color=max_metal=max_rough=0.;triangles=0;duplicates=0
    for key,source in eb.items():
        loaded=ab[key];a,b=canonical_rows(source),canonical_rows(loaded)
        if a.shape!=b.shape:raise AssertionError(f'Triangle multiplicity differs in material/instance partition: {a.shape} != {b.shape}')
        pe=float(np.abs(a[:,:9]-b[:,:9]).max(initial=0))
        if pe>POSITION_TOLERANCE:raise AssertionError(f'Loaded oriented triangle positions changed: {pe}')
        max_p=max(max_p,pe);triangles+=len(a)
        same=np.all(a[1:,:9]==a[:-1,:9],axis=1) if len(a)>1 else np.empty(0,dtype=bool)
        duplicates+=int(same.sum())
        ne=float(np.abs(a[:,9:]-b[:,9:]).max(initial=0))
        if ne>NORMAL_TOLERANCE:
            # Rare duplicate position triples can contain several legitimate
            # normal fields. Match their full N tuples instead of ignoring them.
            starts=np.r_[0,np.flatnonzero(~same)+1,len(a)]
            ne=0.
            for start,end in zip(starts[:-1],starts[1:]):
                an,bn=a[start:end,9:],b[start:end,9:]
                if len(an)==1:
                    error=float(np.max(np.abs(an-bn)))
                    if error>NORMAL_TOLERANCE:raise AssertionError('Authored/smoothed corner normals changed in default load')
                    ne=max(ne,error)
                else:
                    from scipy.optimize import linear_sum_assignment
                    cost=np.max(np.abs(an[:,None,:]-bn[None,:,:]),axis=2)
                    left,right=linear_sum_assignment(np.where(cost<=NORMAL_TOLERANCE,cost,1e6))
                    if np.any(cost[left,right]>NORMAL_TOLERANCE):raise AssertionError('Duplicate-position authored normal multiplicity changed')
                    ne=max(ne,float(cost[left,right].max(initial=0)))
        max_n=max(max_n,ne)
        for sg in source:
            for lg in loaded:
                sm,lm=sg['material'],lg['material']
                color=float(np.max(np.abs(np.asarray(sm['color'])-np.asarray(lm['color']))))
                metal=abs(sm['metallic']-lm['metallic']);rough=abs(sm['roughness']-lm['roughness'])
                if color>COLOR_TOLERANCE:raise AssertionError('PBR baseColorFactor exceeds expected linear 8-bit rounding')
                if max(metal,rough)>1e-12:raise AssertionError('PBR metallic/roughness changed')
                max_color=max(max_color,color);max_metal=max(max_metal,metal);max_rough=max(max_rough,rough)
    return {'triangles':triangles,'source_surface_instances':len(expected),'loaded_surface_instances':len(actual),
            'duplicate_position_triangle_records':duplicates,'material_instance_partitions':len(eb),
            'max_position_error_m':max_p,'max_normal_component_error':max_n,'max_transform_error':frame_error,
            'max_linear_basecolor_rounding_error':max_color,'max_metallic_error':max_metal,'max_roughness_error':max_rough,
            'embedded_pbr_texture_visuals':True,'authored_normal_cache_retained':True}


def verify_glb_scene(path,scene=None):
    expected,digest=raw_groups(path)
    actual=loaded_groups(trimesh.load_scene(path) if scene is None else scene)
    return {'sha256':digest,**compare_groups(expected,actual)}


def verify_urdf_scene(path,model=None):
    path=Path(path).resolve();robot=ET.parse(path).getroot()
    expected=defaultdict(list)
    links={link.get('name') for link in robot.findall('link')}
    for link in robot.findall('link'):
        for visual in link.findall('visual'):
            mesh=visual.find('geometry/mesh')
            if mesh is None:raise ValueError('This runtime audit targets mesh-only robot visuals')
            if not np.array_equal([float(x) for x in mesh.get('scale','1 1 1').split()],[1,1,1]):
                raise ValueError('Non-unit URDF mesh scale needs a scale-aware geometry audit')
            asset=(path.parent/mesh.get('filename')).resolve();origin=urdf_origin(visual.find('origin'))
            groups,_=raw_groups(asset)
            for group in groups:expected[link.get('name'),asset].append({**group,'frame':origin@group['frame']})
    model=yourdfpy.URDF.load(str(path)) if model is None else model
    actual=defaultdict(list)
    for group in loaded_groups(model.scene):
        node=group['node'];parent=model.scene.graph.transforms.parents.get(node)
        if parent not in links:raise AssertionError(f'Loaded geometry is not attached to a source robot link: {node}')
        source=group['mesh'].source
        filename=getattr(source,'file_path',None)
        if filename is None:raise AssertionError('Default loader dropped the geometry source path association')
        asset=Path(filename).resolve()
        frame,_=model.scene.graph.get(node,frame_from=parent)
        actual[parent,asset].append({**group,'frame':frame})
    if expected.keys()!=actual.keys():raise AssertionError('URDF visual link/resource association changed')
    checks=[compare_groups(groups,actual[key]) for key,groups in expected.items()]
    result={'visual_mesh_elements':len(robot.findall('link/visual/geometry/mesh')),'resource_link_groups':len(checks),
            'triangles':sum(c['triangles'] for c in checks),'source_surface_instances':sum(c['source_surface_instances'] for c in checks),
            'loaded_surface_instances':sum(c['loaded_surface_instances'] for c in checks),
            'embedded_pbr_texture_visuals':all(c['embedded_pbr_texture_visuals'] for c in checks),
            'authored_normal_cache_retained':all(c['authored_normal_cache_retained'] for c in checks)}
    for key in ('max_position_error_m','max_normal_component_error','max_transform_error','max_linear_basecolor_rounding_error','max_metallic_error','max_roughness_error'):
        result[key]=max((c[key] for c in checks),default=0.)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--models',type=Path,nargs='*',help='Optional robot model directories, otherwise every robot URDF variant')
    parser.add_argument('--json',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve()
    folders=[(root/p).resolve() for p in args.models] if args.models else [root/'RobotModel']
    if any(not p.is_relative_to(root/'RobotModel') for p in folders):parser.error('Select robot model directories inside RobotModel')
    urdfs=sorted({u for folder in folders for u in folder.rglob('*.urdf')})
    if not urdfs:parser.error('No robot URDFs selected')
    refs=sorted({(u.parent/m.get('filename')).resolve() for u in urdfs for m in ET.parse(u).findall('link/visual/geometry/mesh')})
    snapshot_started=datetime.now(timezone.utc).isoformat()
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*urdfs,*refs]}
    snapshot_completed=datetime.now(timezone.utc).isoformat()
    manifest={Path(p).relative_to(root).as_posix():digest for p,digest in hashes.items()}
    manifest_sha=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'versions':{'trimesh':trimesh.__version__,'yourdfpy':yourdfpy.__version__},
            'checker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_snapshot_started_at':snapshot_started,'source_snapshot_completed_at':snapshot_completed,
            'input_manifest_sha256':manifest_sha,
            'manifest_encoding':'SHA-256 of compact sorted-key JSON {repository-relative input path: SHA-256}; all visual GLBs and URDF variants included',
            'load_settings':'Default trimesh.load_scene(path) and yourdfpy.URDF.load(path); no force_mesh/process/visual-color overrides',
            'profile':'Actual robot URDF visual GLBs only, default GLB scenes, authored P/N triangles, static/untextured, no explicit non-unit mesh scale',
            'comparison':'Oriented triangle position+normal multisets partitioned by PBR and instance transform; cyclic face rotations/vertex reindexing allowed; duplicate multiplicities retained',
            'tolerances':{'position_m':POSITION_TOLERANCE,'normal_component':NORMAL_TOLERANCE,'transform':FRAME_TOLERANCE,'linear_basecolor':COLOR_TOLERANCE,'metallic_roughness':1e-12},
            'scope_note':'Checks loaded PBR/N data and visual transforms relative to URDF links. Does not verify world forward kinematics or whether downstream renderers implement PBR/glTF culling.',
            'glbs':[],'urdfs':[]}
    for path in refs:
        row={'path':path.relative_to(root).as_posix()}
        try:row.update(status='passed',**verify_glb_scene(path))
        except Exception as exc:row.update(status='failed',error=str(exc));print('FAIL',row['path'],exc,flush=True)
        report['glbs'].append(row)
    for path in urdfs:
        row={'path':path.relative_to(root).as_posix(),'sha256':hashes[str(path)]}
        try:row.update(status='passed',**verify_urdf_scene(path))
        except Exception as exc:row.update(status='failed',error=str(exc));print('FAIL',row['path'],exc,flush=True)
        report['urdfs'].append(row)
        print('Loaded',row['path'],row['status'],flush=True)
    changed=[p for p,h in hashes.items() if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h]
    all_rows=report['glbs']+report['urdfs']
    report['summary']={'visual_glbs':len(refs),'urdf_variants':len(urdfs),'errors':sum(r['status']!='passed' for r in all_rows)+len(changed),
                       'triangles_in_unique_visual_glbs':sum(r.get('triangles',0) for r in report['glbs']),
                       'triangles_in_loaded_urdf_instances':sum(r.get('triangles',0) for r in report['urdfs']),
                       'urdf_visual_mesh_elements':sum(r.get('visual_mesh_elements',0) for r in report['urdfs']),
                       'source_assets_changed_during_validation':changed,'pbr_and_authored_normals_retained':not changed and all(r['status']=='passed' for r in all_rows)}
    for key in ('max_position_error_m','max_normal_component_error','max_transform_error','max_linear_basecolor_rounding_error','max_metallic_error','max_roughness_error'):
        report['summary'][key]=max((r.get(key,0.) for r in all_rows),default=0.)
    report['completed_at']=datetime.now(timezone.utc).isoformat()
    args.json.parent.mkdir(parents=True,exist_ok=True);args.json.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report['summary']),flush=True)
    return int(report['summary']['errors']>0)


if __name__=='__main__':raise SystemExit(main())
