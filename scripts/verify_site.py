"""Portable local/Actions packaging smoke test. Does not publish or fetch quotes."""
from pathlib import Path
import ast
import json
import re

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    required = ('index.html','app.js','portfolio.js','demo.js','styles.css','favicon.svg',
                'scripts/update_data.py','scripts/strategy.py','scripts/adaptive.py','scripts/providers.py',
                'config/strategy.json','.github/workflows/check.yml',
                '.github/workflows/update-data.yml','.github/workflows/deploy-pages.yml')
    missing = [p for p in required if not (root/p).is_file()]
    if missing:
        raise ValueError('Missing root files: ' + ', '.join(missing))
    for source in (root/'scripts').glob('*.py'):
        ast.parse(source.read_text(encoding='utf-8'), filename=str(source), feature_version=(3,11))
    for source in ('app.js','portfolio.js','demo.js'):
        text = (root/source).read_text(encoding='utf-8')
        for imported in re.findall(r"from\s+['\"](\./[^'\"]+)['\"]",text):
            if not (root/imported).is_file():
                raise ValueError(f'Broken module {source}: {imported}')
    html = (root/'index.html').read_text(encoding='utf-8')
    for asset in re.findall(r'(?:src|href)="([^"#]+)"',html):
        if asset.startswith(('http:', 'https:')):
            continue
        if asset.startswith('/') or not (root/asset.split('?')[0]).is_file():
            raise ValueError(f'Project Pages relative asset failed: {asset}')
    for source in (root/'data').glob('*.json'):
        json.loads(source.read_text(encoding='utf-8-sig'))
    config = json.loads((root/'config/strategy.json').read_text())
    assert config['candidateSessions']==config['holdingSessions']==7
    assert config['adaptivePricing']['enabled'] and config['selectionRules']['includeTrendPullbacks']
    assert config['selectionRules']['strictAdmission']
    return {'status':'PASS','pythonCompatibility':'3.11 syntax','rootIndex':True,'relativeAssets':True,
            'parameterVersion':config['parameterVersion'],'automaticOrders':False}


if __name__=='__main__':
    print(json.dumps(verify()))
