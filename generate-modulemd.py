#!/usr/bin/python3
# Author: Mikolaj Izdebski <mizdebsk@redhat.com>

import os
import re
import logging
import koji
import hawkey

from koschei import config
from koschei.backend import koji_util, repo_util, depsolve, repo_cache

log = logging.getLogger('')

bootstrap = False
full_refs = True

api = ['maven']
profiles = {'default': ['maven']}
includes = ['python-lxml', 'byaccj']
excludes = ['java-1.7.0-openjdk', 'java-1.8.0-openjdk']
default_ref = None
frozen_refs = ['python-lxml']
stream_override = {'java': {'f27': '8'}}

macros = {
    '_with_xmvn_javadoc': 1,
    '_without_asciidoc': 1,
    '_without_avalon': 1,
    '_without_bouncycastle': 1,
    '_without_cython': 1,
    '_without_dafsa': 1,
    '_without_desktop': 1,
    '_without_doxygen': 1,
    '_without_dtd': 1,
    '_without_eclipse': 1,
    '_without_ehcache': 1,
    '_without_emacs': 1,
    '_without_equinox': 1,
    '_without_fop': 1,
    '_without_ftp': 1,
    '_without_gradle': 1,
    '_without_groovy': 1,
    '_without_hadoop': 1,
    '_without_hsqldb': 1,
    '_without_itext': 1,
    '_without_jackson': 1,
    '_without_jmh': 1,
    '_without_jna': 1,
    '_without_jpa': 1,
    '_without_logback': 1,
    '_without_markdown': 1,
    '_without_memcached': 1,
    '_without_memoryfilesystem': 1,
    '_without_obr': 1,
    '_without_python': 1,
    '_without_reporting': 1,
    '_without_scm': 1,
    '_without_snappy': 1,
    '_without_spring': 1,
    '_without_ssh': 1,
    '_without_testlib': 1,
}

with open('.git/HEAD') as f:
    stream = re.match(r'ref: refs/heads/(.*)', f.readlines()[0]).group(1)

config.load_config(['koschei.cfg'], ignore_env=True)
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
        for srpm, build_requires in zip(srpms_todo, get_build_requires(srpms_todo)):
            combined_br |= set(build_requires)
            for br in build_requires:
                add(br_map, br, srpm)
        java, all = resolve_deps(sack, combined_br)
        srpms_todo |= java
        srpms_todo -= srpms_done
        pkgs |= all

    def our(pkgs):
        return (pkg for pkg in pkgs if pkg.sourcerpm in srpms_done)

    api_srpms = {pkg.sourcerpm for pkg in pkgs if pkg.name in api}

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
    yaml.append('    summary: Java project management and project comprehension tool')
    yaml.append('    description: >-')
    yaml.append('        Maven is a software project management and comprehension tool.')
    yaml.append('        Based on the concept of a project object model (POM), Maven')
    yaml.append('        can manage a project\'s build, reporting and documentation from')
    yaml.append('        a central piece of information.')
    yaml.append('    license:')
    yaml.append('        module:')
    yaml.append('            - MIT')
    yaml.append('    dependencies:')
    yaml.append('        buildrequires:')
    if bootstrap:
        yaml.append('            bootstrap: master')
    else:
        yaml.append('            maven: master')
        yaml.append('            java: master')
        yaml.append('            platform: master')
        yaml.append('            # R of hawtjni')
        yaml.append('            autotools: master')
        yaml.append('            # BR of python-lxml')
        yaml.append('            python2: master')
        yaml.append('            python3: master')
        yaml.append('            # BR of xml-stylebook')
        yaml.append('            fonts: master')
    yaml.append('        requires:')
    for dep in ('java', 'platform'):
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
    yaml.append('    filter:')
    yaml.append('        rpms:')
    yaml.append('            - python2-lxml')
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

    with open('maven.yaml', 'w') as f:
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
