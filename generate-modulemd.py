#!/usr/bin/python3
#
# Copyright (c) 2017-2018 Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# Author: Mikolaj Izdebski <mizdebsk@redhat.com>

import os
import re
import logging
import koji
import hawkey

from koschei import config
from koschei.backend import koji_util, repo_util, depsolve, repo_cache

log = logging.getLogger('')

# Use git repo name and branch for module/stream.
module = os.path.basename(os.getcwd())
with open('.git/HEAD') as f:
    stream = re.match(r'ref: refs/heads/(.*)', f.readlines()[0]).group(1)

config.load_config(['koschei.cfg'], ignore_env=True)

bootstrap = config.get_config('bootstrap')
full_refs = config.get_config('full_refs')
default_ref = config.get_config('default_ref')
include_build_deps = config.get_config('include_build_deps')
api = config.get_config('api')
profiles = config.get_config('profiles')
includes = config.get_config('includes')
excludes = config.get_config('excludes')
frozen_refs = config.get_config('frozen_refs')
stream_override = config.get_config('stream_override')
macros = config.get_config('macros')

ks = koji_util.KojiSession()

tag_name = config.get_koji_config('primary', 'tag_name')
repo_id = ks.getRepo(tag_name, state=koji.REPO_READY)['id']
repo_descriptor = repo_util.KojiRepoDescriptor('primary', tag_name, repo_id)


# Parse RPM name - split it into NVRA dict
def parse_nvra(str):
    m = re.match(r'^(.+)-([^-]+)-([^-]+)\.([^-.]+).rpm$', str)
    return {'name': m.group(1), 'version': m.group(2),
            'release': m.group(3), 'arch': m.group(4)}


# Get RPM name
def name(rpm):
    return parse_nvra(rpm)['name']


# Try to heuristically guess whether given hawkey package is Java package and
# should be part of maven module, or not. Explicit includes and excludes always
# have preference over heuristic.
def is_maven_pkg(pkg):
    if name(pkg.sourcerpm) in includes:
        return True
    if name(pkg.sourcerpm) in excludes:
        return False
    for file in pkg.files:
        if file.startswith('/usr/share/maven-metadata/'):
            return True
        if file.startswith('/usr/share/java/'):
            return True
        if file.startswith('/usr/lib/java/'):
            return True
    return False


# Simulate installation of given dependencies.
def resolve_deps(sack, deps):
    log.info('Resolving deps...')
    resolved, problems, installs = depsolve.run_goal(sack, deps, [])
    if not resolved:
        installs = set()
        for pkg in deps:
            resolved, problems, pkg_installs = \
                depsolve.run_goal(sack, [pkg], [])
            if resolved:
                installs.update(pkg_installs)
            else:
                log.warning('Dependency problems for {}:\n{}'
                            .format(pkg, '\n'.join(problems)))
    java = {pkg.sourcerpm for pkg in installs if is_maven_pkg(pkg)}
    return java, set(installs)


# Input: BuildRequires string
# Output: matched hawkey packages
def resolve_builddep(sack, br):
    return depsolve._get_builddep_selector(sack, br).matches()


# Input: hawkey reldep
# Output: matched hawkey packages
def resolve_reldep(sack, reldep):
    return hawkey.Query(sack).filter(provides=reldep)


# Get build-requires of given SRPMs from Koji.
def get_build_requires(srpms):
    log.info('Getting build-requires from Koji...')
    return koji_util.get_rpm_requires(ks, [parse_nvra(srpm) for srpm in srpms])


# Get names of binary RPMs corresponding to given list of source RPMs.
def get_binary_rpms(srpms):
    rpm_names = set()
    builds = koji_util.itercall(ks, list(srpms), lambda ks, srpm: ks.getBuild(parse_nvra(srpm)))
    rpms_gen = koji_util.itercall(ks, list(builds), lambda ks, build: ks.listRPMs(build['id'], arches=('noarch', 'x86_64')))
    for rpms in rpms_gen:
        rpm_names.update([rpm['name'] for rpm in rpms if not rpm['name'].endswith('-debuginfo') and not rpm['name'].endswith('-debugsource')])
    return rpm_names


# For each SRPM, figure out from which git commit it was built.
def resolve_refs(srpms):
    def get_ref(children):
        for task in children:
            if task['label'] == 'srpm':
                log = ks.downloadTaskOutput(task['id'], 'checkout.log')
                for line in log.decode('utf-8').splitlines():
                    if line.startswith('HEAD is now at'):
                        return line[15:22]
    builds = koji_util.itercall(ks, list(srpms), lambda ks, srpm: ks.getBuild(parse_nvra(srpm)))
    children_gen = koji_util.itercall(ks, list(builds), lambda ks, build: ks.getTaskChildren(build['task_id']))
    return {srpm: get_ref(children) for srpm, children in zip(srpms, children_gen)}


# Do the main work. Recursively resolve requires and build-requires, starting
# from initial list of seed packages.
def work(sack):
    srpms_done = set()
    br_map = {}
    srpms_todo, pkgs = resolve_deps(sack, api)

    def add(map, key, val):
        _set = map.get(key, set())
        map[key] = _set
        _set.add(val)

    while srpms_todo:
        log.info('Round: {} packages'.format(len(srpms_todo)))
        srpms_todo = {srpm for srpm in srpms_todo
                      if name(srpm) not in excludes}
        srpms_done |= srpms_todo
        combined_br = set()
        if include_build_deps:
            for srpm, build_requires in zip(srpms_todo, get_build_requires(srpms_todo)):
                combined_br |= set(build_requires)
                for br in build_requires:
                    add(br_map, br, srpm)
        if config.get_config('closure'):
            combined_br.update(get_binary_rpms(srpms_todo))
        java, all = resolve_deps(sack, combined_br)
        srpms_todo |= java
        srpms_todo -= srpms_done
        pkgs |= all

    def our(pkgs):
        return (pkg for pkg in pkgs if pkg.sourcerpm in srpms_done)

    api_srpms = {pkg.sourcerpm for pkg in pkgs if pkg.name in api}

    filtered = set(config.get_config('filter'))
    if config.get_config('filter_unused'):
        filtered.update(get_binary_rpms(srpms_done) - {pkg.name for pkg in pkgs})

    build_deps = {}
    for br, srpms in br_map.items():
        for dep in our(resolve_builddep(sack, br)):
            for srpm in srpms:
                add(build_deps, dep.sourcerpm, srpm)

    runtime_deps = {}
    for pkg in our(pkgs):
        for reldep in pkg.requires:
            for dep in resolve_reldep(sack, reldep):
                if dep.sourcerpm != pkg.sourcerpm:
                    add(runtime_deps, dep.sourcerpm, pkg.sourcerpm)

    yaml = list()
    yaml.append('---')
    yaml.append('document: modulemd')
    yaml.append('version: 1')
    yaml.append('data:')
    yaml.append('    summary: {}'.format(config.get_config('summary')))
    yaml.append('    description: >-')
    for line in config.get_config('description'):
        yaml.append('        {}'.format(line))
    yaml.append('    license:')
    yaml.append('        module:')
    yaml.append('            - MIT')
    yaml.append('    dependencies:')
    yaml.append('        buildrequires:')
    if bootstrap:
        yaml.append('            bootstrap: master')
    else:
        for dep in config.get_config('buildrequires'):
            dep_stream = stream_override.get(dep, {}).get(stream, stream)
            yaml.append('            {}: {}'.format(dep, dep_stream))
    yaml.append('        requires:')
    for dep in config.get_config('requires'):
        dep_stream = stream_override.get(dep, {}).get(stream, stream)
        yaml.append('            {}: {}'.format(dep, dep_stream))
    yaml.append('    profiles:')
    for k, v in profiles.items():
        yaml.append('        {}:'.format(k))
        yaml.append('            rpms:')
        for p in sorted(v):
            yaml.append('                - {}'.format(p))
    yaml.append('    api:')
    yaml.append('        rpms:')
    for p in sorted(api):
        yaml.append('            - {}'.format(p))
    if filtered:
        yaml.append('    filter:')
        yaml.append('        rpms:')
        for rpm in sorted(filtered):
            yaml.append('            - {}'.format(rpm))
    yaml.append('    buildopts:')
    yaml.append('        rpms:')
    yaml.append('            macros: |')
    for k, v in macros.items():
        yaml.append('                %{} {}'.format(k, v))
    yaml.append('    components:')
    yaml.append('        rpms:')

    def format_rationale(type, srpms):
        rt = '                    {} of'.format(type)
        for srpm in sorted(srpms):
            r = name(srpm)
            if len(rt + ' ' + r + ',') >= 80:
                yaml.append(rt)
                rt = '                        '
            rt += ' ' + r + ','
        rt = rt[:-1] + '.'
        yaml.append(rt)

    log.info('Resolving git refs...')
    if full_refs:
        refs = resolve_refs(srpms_done)
    else:
        refs = resolve_refs([srpm for srpm in srpms_done if name(srpm) in frozen_refs])

    for srpm in sorted(srpms_done):
        yaml.append('            # {}'.format(srpm[:-8]))
        yaml.append('            {}:'.format(name(srpm)))
        ref = refs.get(srpm, default_ref)
        if ref:
            yaml.append('                ref: {}'.format(ref))
        yaml.append('                rationale: >')
        if srpm in api_srpms:
            yaml.append('                    Module API.')
        if srpm in runtime_deps:
            format_rationale('Runtime dependency', runtime_deps[srpm])
        if srpm in build_deps:
            format_rationale('Build dependency', build_deps[srpm])

    with open('{}.yaml'.format(module), 'w') as f:
        for line in yaml:
            print(line, file=f)


def main():
    if not os.path.exists('/tmp/maven-modulemd-gen/repodata'):
        os.makedirs('/tmp/maven-modulemd-gen/repodata')
    log.info('Loading sack...')
    with repo_cache.RepoCache().get_sack(repo_descriptor) as sack:
        work(sack)


if __name__ == '__main__':
    main()
