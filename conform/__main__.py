import argparse
import json
from pathlib import Path
from .exporter import build_bundle
from .fcp7 import BYPASS
from .naming import Settings, DEFAULT_TEMPLATE


def main():
    p = argparse.ArgumentParser(description='Conform a Resolve FCP7 XML export offline (DRT needs Resolve).')
    p.add_argument('xml', type=Path, help='FCP7 XML exported from Resolve')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--renders', type=Path, help='Folder with the rendered clips; omit to cut media paths')
    p.add_argument('--bypass', default='', help='Comma list: ' + ','.join(BYPASS))
    p.add_argument('--timeline', default='timeline')
    p.add_argument('--template', default=DEFAULT_TEMPLATE)
    p.add_argument('--prefix', default='V', help='Track prefix: V -> V1, V2, ...')
    args = p.parse_args()
    settings = Settings(args.template, args.prefix)
    dest, names, warnings = build_bundle(args.xml, args.output, args.timeline, settings,
                                         renders=args.renders, bypass=[b for b in args.bypass.split(',') if b])
    print(json.dumps(dict(files=[str(p) for p in dest], clips=len(names), warnings=warnings), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
