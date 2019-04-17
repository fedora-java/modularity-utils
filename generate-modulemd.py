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
# Author: Michael Simacek <msimacek@redhat.com>

import os
import re
import logging
import koji
import hawkey
import jinja2

from textwrap import dedent, fill

from koschei import config
try:
    from koschei.backend.koji_util import KojiRepoDescriptor
except ImportError:
    from koschei.backend.repo_util import KojiRepoDescriptor
from koschei.backend import koji_util, depsolve, repo_cache

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
macros = config.get_config('macros', None)

ks = koji_util.KojiSession()

tag_name = config.get_koji_config('primary', 'tag_name')
repo_id = ks.getRepo(tag_name, state=koji.REPO_READY)['id']
repo_descriptor = KojiRepoDescriptor('primary', tag_name, repo_id)


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


def topo_sort(V, E):
    W = {}
    i = 1
    while V:
        U = set()
        for v in V:
            for u in V:
                if v in E.get(u, ()):
                    break
            else:
                W[v] = i
                U.add(v)
        if not U:
            log.error('There are dependency cycles, topological sort is not possible')
            return None
        V = V - U
        i += 1
    return W


# For each SRPM, figure out from which git commit it was built.
def resolve_refs(srpms):
    def get_ref(build):
        scm_url = build['extra']['source']['original_url']
        match = re.search(r'#([0-9a-f]{7})[0-9a-f]*$', scm_url)
        assert match
        return match.group(1)
    builds = koji_util.itercall(ks, list(srpms), lambda ks, srpm: ks.getBuild(parse_nvra(srpm)))
    return {srpm: get_ref(build) for srpm, build in zip(srpms, builds)}


# Do the main work. Recursively resolve requires and build-requires, starting
# from initial list of seed packages.
def work(sack):
    filtered = set(config.get_config('filter'))
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
            combined_br.update(get_binary_rpms(srpms_todo) - filtered)
        java, all = resolve_deps(sack, combined_br)
        srpms_todo |= java
        srpms_todo -= srpms_done
        pkgs |= all

    def our(pkgs):
        return (pkg for pkg in pkgs if pkg.sourcerpm in srpms_done)

    api_srpms = {pkg.sourcerpm for pkg in pkgs if pkg.name in api}

    if filtered:
        for pkg in filtered - get_binary_rpms(srpms_done):
            log.warning('Filtered RPM {} was not found'.format(pkg))

    if config.get_config('filter_unused'):
        filtered.update(get_binary_rpms(srpms_done) - {pkg.name for pkg in pkgs})

    def pretty_rpm_name(rpm):
        if rpm.name == name(rpm.sourcerpm):
            return rpm.name
        else:
            return '{} (subpackage of {})'.format(rpm.name, name(rpm.sourcerpm))

    build_deps = {}
    for br, srpms in br_map.items():
        for dep in our(resolve_builddep(sack, br)):
            for srpm in srpms:
                if dep.name in filtered:
                    log.warning('Build dependency broken by filter: component {} BuildRequires "{}", which pulls in filtered RPM {}.'
                                .format(name(srpm), br, pretty_rpm_name(dep)))
                add(build_deps, dep.sourcerpm, srpm)

    runtime_deps = {}
    for pkg in our(pkgs):
        for reldep in pkg.requires:
            for dep in resolve_reldep(sack, reldep):
                if dep.name in filtered and pkg.name not in filtered:
                    log.warning('Runtime dependency broken by filter: package {} Requires "{}", which pulls in filtered RPM {}.'
                                .format(pretty_rpm_name(pkg), reldep, pretty_rpm_name(dep)))
                if dep.sourcerpm != pkg.sourcerpm:
                    add(runtime_deps, dep.sourcerpm, pkg.sourcerpm)

    if config.get_config('topo_sort', False):
        buildorder = topo_sort(srpms_done, {**runtime_deps, **build_deps})

    log.info('Resolving git refs...')
    if full_refs:
        refs = resolve_refs(srpms_done)
    else:
        refs = resolve_refs([srpm for srpm in srpms_done if name(srpm) in frozen_refs])

    def get_stream(dep):
        return stream_override.get(dep, {}).get(stream, stream)

    yaml = dedent("""\
    {% macro format_rationale(type, srpms, cont_indent) %}
    {% filter wrap(width=(80 - cont_indent)) | indent(cont_indent) %}
    {{ type }} of {{ sorted(srpms) | map('name') | join(', ') }}.
    {% endfilter %}
    {%- endmacro %}
    ---
    document: modulemd
    version: 2
    data:
        summary: {{ config.get_config('summary') }}
        description: >-
    {{ config.get_config('description').rstrip() }}
        license:
            module:
                - MIT
        dependencies:
            - buildrequires:
                  {% if bootstrap %}
                  bootstrap: master
                  {% else %}
                  {% for dep in config.get_config('buildrequires') %}
                  {{ dep }}: {{ get_stream(dep) }}
                  {% endfor %}
                  {% endif %}
              requires:
                  {% for dep in config.get_config('requires') %}
                  {{ dep }}: {{ get_stream(dep) }}
                  {% endfor %}
        profiles:
            {% for profile, content in profiles.items() %}
            {{ profile }}:
                rpms:
                    {% for package in sorted(content) %}
                    - {{ package }}
                    {% endfor %}
            {% endfor %}
        api:
            rpms:
                {% for package in sorted(api) %}
                - {{ package }}
                {% endfor %}
        {% if filtered %}
        filter:
            rpms:
                {% for rpm in sorted(filtered) %}
                - {{ rpm }}
                {% endfor %}
        {% endif %}
        {% if macros %}
        buildopts:
            rpms:
                macros: |
                    {% for macro, value in macros.items() %}
                    %{{ macro }} {{ value }}
                    {% endfor %}
        {% endif %}
        components:
            rpms:
                {% for srpm in sorted(srpms_done) %}
                {{ srpm | name }}:
                    {% if buildorder is defined %}
                    buildorder: {{ buildorder[srpm] }}0
                    {% endif %}
                    {% set ref = refs.get(srpm, default_ref) %}
                    {% if ref %}
                    ref: {{ ref }}
                    {% endif %}
                    rationale: >
                        {% if srpm in api_srpms %}
                        Module API.
                        {% endif %}
                        {% if srpm in runtime_deps %}
                        {{ format_rationale('Runtime dependency', runtime_deps[srpm], 25) }}
                        {% endif %}
                        {% if srpm in build_deps %}
                        {{ format_rationale('Build dependency', build_deps[srpm], 25) }}
                        {% endif %}
                {% endfor %}
    """)

    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
    env.globals.update(vars(__builtins__))
    env.globals.update(globals())
    env.filters['name'] = name

    def wrap(text, width):
        return fill(text, width, break_on_hyphens=False, break_long_words=False)

    env.filters['wrap'] = wrap
    content = env.from_string(yaml).render(**locals())

    with open('{}.yaml'.format(module), 'w') as f:
        f.write(content)


def main():
    if not os.path.exists('/tmp/maven-modulemd-gen/repodata'):
        os.makedirs('/tmp/maven-modulemd-gen/repodata')
    log.info('Loading sack...')
    with repo_cache.RepoCache().get_sack(repo_descriptor) as sack:
        work(sack)


if __name__ == '__main__':
    main()
