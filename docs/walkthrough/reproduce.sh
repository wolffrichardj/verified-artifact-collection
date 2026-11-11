#!/bin/sh
# Python 3.11+, Git; supported macOS/Linux POSIX filesystem.
# Leaves its fresh temporary workspace for inspection, prints its location.
set -eu
workspace=$(mktemp -d "${TMPDIR:-/tmp}/collection-walkthrough.XXXXXX")
printf 'Synthetic reproduction workspace: %s\n' "$workspace"
git clone --quiet https://github.com/wolffrichardj/verified-artifact-collection.git "$workspace/source"
git -C "$workspace/source" checkout --quiet --detach df5efa8c1cc118a12746e69e05c9537317c577dc
cd "$workspace/source"
test "$(git rev-parse HEAD)" = df5efa8c1cc118a12746e69e05c9537317c577dc
python3 -B -m unittest discover -s . -p test_sample.py -v
records="$workspace/records"
mkdir "$records"
python3 -B - "$records" <<'PY'
import hashlib,json,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]);results=[];snapshots=[]
for scenario,expected in [('happy',0),('happy',0),('revised',0),('wrong-identity',1),('tampered-source',1)]:
    cmd=[sys.executable,'-B','sample.py','--root',str(root),'--scenario',scenario]
    p=subprocess.run(cmd,capture_output=True,text=True)
    print('scenario:',scenario,'exit:',p.returncode)
    print(p.stdout,end='')
    if p.stderr: print(p.stderr,file=sys.stderr,end='')
    assert p.returncode==expected,(scenario,p.returncode)
    results.append(json.loads(p.stdout))
    snapshots.append({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(root.iterdir()) if f.is_file()})
assert [x['landing']['disposition'] for x in results[:3]]==['created','duplicate','variant']
assert all(x['domain_ok'] is True and x['physical_verified'] is True and x['landing']['readback_verified'] is True for x in results[:3])
assert results[3]=={'domain_ok':False,'error':'collection_rejected'}
assert results[4]=={'domain_ok':False,'error':'tamper_rejected'}
assert snapshots[0]['statement-0001.txt']==snapshots[2]['statement-0001.txt']
assert len(snapshots[2])==2 and len(set(snapshots[2].values()))==2
assert snapshots[2]==snapshots[3]==snapshots[4]
print('PASS: original preserved, two distinct versions retained, rejection inventories unchanged.')
PY
test -z "$(git status --porcelain --untracked-files=all)"
