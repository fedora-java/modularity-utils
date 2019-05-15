#!/usr/bin/python3
#
# Copyright (c) 2019 Red Hat, Inc.
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
# Author: Marian Koncek <mkoncek@redhat.com>

import datetime
import koji
import re
import requests
import rpm

from concurrent.futures import ThreadPoolExecutor as thread_pool

fedora_releases = ["f28", "f29", "f30", "f31"]
releases = fedora_releases + ["mbi", "upstream"]
mbi_index = len(fedora_releases)
upstream_index = mbi_index + 1

exceptions = list()

with open("exceptions", "r") as exception_file:
	for line in exception_file.readlines():
		exceptions.append(line)

def get_packages() -> [str]:
	ks = koji.ClientSession("https://koji.kjnet.xyz/kojihub")
	return sorted([package["package_name"] for package in filter(lambda package: not package["blocked"], ks.listPackages("jp"))])

def get_upstream_version(package_name: str) -> str:
	project_items = requests.get(str.format(
		"https://release-monitoring.org/api/v2/packages/?name={}&distribution=Fedora", package_name
	)).json()["items"]
	
	if len(project_items) == 0:
		return ""
	
	project_name = project_items[0]["project"]
	
	return requests.get(str.format(
		"https://release-monitoring.org/api/v2/projects/?name={}", project_name
	)).json()["items"][0]["version"]

def get_upstream_versions(package_names: [str]) -> {str: str}:
	result = {}
	
	pool = thread_pool(30)
	futures = list()
	
	for package_name in package_names:
		futures.append(pool.submit(get_upstream_version, package_name))
		
	for package_name, project_version in zip(package_names, futures):
		result[package_name] = project_version.result()
		
	return result

def get_koji_versions(package_names: [str], url: str, tag: str) -> {str : str}:
	ks = koji.ClientSession(url)
	result = {build["package_name"]: build["version"] for build in
		filter(lambda buildinfo: buildinfo["package_name"] in package_names, ks.listTagged(tag, latest = True))}
	for package_name in package_names:
		if package_name not in result.keys():
			result[package_name] = str()
	return result

def get_fedora_versions(package_names: [str], release: str) -> {str: str}:
	return get_koji_versions(package_names, "https://koji.fedoraproject.org/kojihub", release)

def get_mbi_versions(package_names: [str]) -> {str: str}:
	return get_koji_versions(package_names, "https://koji.kjnet.xyz/kojihub", "jp")

def get_all_versions() -> {str: []}:
	result = {}
	
	package_names = get_packages()
	
	upstream = get_upstream_versions(package_names)
	mbi = get_mbi_versions(package_names)
	releases = {}
	
	pool = thread_pool(len(fedora_releases))
	futures = list()
	
	for release in fedora_releases:
		futures.append(pool.submit(get_fedora_versions, package_names, release))
		
	for release, release_versions in zip(fedora_releases, futures):
		releases[release] = release_versions.result()
		
	for package_name in package_names:
		result[package_name] = []
		for release in fedora_releases:
			result[package_name].append(releases[release][package_name])
		result[package_name].append(mbi[package_name])
		result[package_name].append(upstream[package_name])
		
	return result

def version_compare(left: str, right: str) -> int:
	return rpm.labelCompare(("", left, ""), ("", right, ""))

def row_to_str(versions : [str]) -> str:
	assert(len(versions) == len(releases))
	
	result = str()
	html_class = str()
	fedora_index = 0
	
	while fedora_index < mbi_index:
		colspan = 1
		while fedora_index + 1 < mbi_index and version_compare(versions[fedora_index], versions[fedora_index + 1]) == 0:
			colspan += 1
			fedora_index += 1
			
		html_class = "fedora"
		result += '<td '
		
		if colspan > 1:
			result += 'colspan="' + str(colspan) + '" '
			
		result += 'class="' + html_class + '">' + versions[fedora_index] + '</td>\n'
		fedora_index += 1
		
	html_class = "mbi"
	result += '<td class="' + html_class + '">' + versions[mbi_index] + '</td>\n'
	
	compare_value = version_compare(versions[mbi_index], versions[upstream_index])
	if versions[upstream_index] == "":
		html_class = "unknown-version"
	elif compare_value == 0:
		html_class = "up-to-date"
	elif compare_value < 0:
		html_class = "downgrade"
	elif compare_value > 1:
		html_class = "mbi-newer"
	result += '<td class="' + html_class + '">' + versions[upstream_index] + '</td>\n'
	
	return result

versions_all = get_all_versions()

with open("versions-" + datetime.datetime.now().strftime("%G-%m-%d_%H:%M:%S") + ".html", "w") as table:
	table.write('<link rel=stylesheet href=mystyle.css>')
	table.write('<table style="width:100%">\n')
	table.write('<th>' + 'Package name' + '</th>')
	
	for header_name in releases:
		table.write('<th>' + header_name + '</th>')
	
	for pkg_name, version_list in versions_all.items():
		table.write('<tr>\n')
		table.write('<td')
		if pkg_name in exceptions:
			table.write(' class=exception')
		table.write('>' + pkg_name + '</td>\n')
		table.write(row_to_str(version_list))
		table.write('</tr>\n')
	table.write('</table>\n')
