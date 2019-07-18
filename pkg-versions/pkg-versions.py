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

import json
import koji
import os
import markdown2
import re
import requests
import rpm
import time

from concurrent.futures import ThreadPoolExecutor as thread_pool

################################################################################

# If the cache file is older than this time, regenerate it
upstream_cache_interval = 1 * 60 * 60
upstream_cache_path = "/tmp/pkg-versions-upstream-cache.json"

fedora_releases = ["f28", "f29", "f30", "f31"]
releases = fedora_releases + ["mbi", "upstream"]

mbi_index = len(fedora_releases)
upstream_index = mbi_index + 1

thread_pool_size = 30

################################################################################

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
	
	pool = thread_pool(thread_pool_size)
	futures = list()
	
	for package_name in package_names:
		futures.append(pool.submit(get_upstream_version, package_name))
	
	for package_name, project_version in zip(package_names, futures):
		result[package_name] = project_version.result()
	
	return result

def read_json(filename: str) -> {str: str}:
	with open(filename, "r") as cache:
		return json.load(cache)

def write_json_timestamp(filename: str, packages: {str: str}):
	with open(filename, "w") as cache:
		result = {"time-retrieved": time.time(), "packages": packages}
		json.dump(result, cache, indent = 0)
		cache.write("\n")

def get_upstream_versions_cached(cache_path: str, package_names: [str]) -> {str: str}:
	update_cache = False
	result = {}
	
	if not os.path.exists(cache_path):
		update_cache = True
		
	else:
		cache = read_json(cache_path)
		
		if time.time() - cache["time-retrieved"] > upstream_cache_interval:
			update_cache = True
		
		else:
			result = cache["packages"]
	
	if update_cache:
		result = get_upstream_versions(package_names)
		write_json_timestamp(cache_path, result)
	
	return result

def get_koji_versions(package_names: [str], url: str, tag: str) -> {str : str}:
	ks = koji.ClientSession(url)
	ks.multicall = True
	for pkg in package_names:
		ks.listTagged(tag, package=pkg, latest=True)
	result = {}
	for [builds] in ks.multiCall(strict=True):
		if builds:
			result[builds[0]['package_name']] = builds[0]['version']
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
	
	upstream = get_upstream_versions_cached(upstream_cache_path, package_names)
	upstream = {package: normalize_version(version) for package, version in upstream.items()}
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

def normalize_version(version: str) -> str:
	if not version:
		return ""
	
	version_name = version[:]
	version_name = version_name.replace("_", ".")
	version_name = version_name.replace("-", ".")
	
	# Match classical version symbols (numbers and dot)
	match = re.match("([.0-9]*[0-9]+)(.*)", version_name)
	
	if not match:
		raise BaseException("Invalid version name: " + version_name)
	
	leading = match.group(1)
	trailing = match.group(2)
	
	if trailing == ".Final":
		return leading
	
	# If the proper version is followed by a single letter, keep it
	# Use tilde split otherwise
	if not re.match("^[a-zA-Z]$", trailing):
		if trailing:
			if trailing.startswith((".", "~")):
				trailing = trailing[1:]
			
			# Service pack post-release should not use pre-release tilde
			if trailing.startswith("SP"):
				trailing = "." + trailing
			
			else:
				trailing = "~" + trailing
		
		trailing = trailing.replace("-", ".")
	
	return leading + trailing

def version_compare(left: str, right: str) -> int:
	return rpm.labelCompare(("", left, ""), ("", right, ""))

def row_to_str(versions : [str], tags : {str : str}) -> str:
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
	elif "correct_version" in tags and version_compare(versions[mbi_index], tags["correct_version"]) == 0:
		html_class = "correct_version"
	elif compare_value == 0:
		html_class = "up-to-date"
	elif compare_value < 0:
		html_class = "downgrade"
	elif compare_value > 0:
		html_class = "mbi-newer"
	
	result += '<td class="' + html_class + '">' + versions[upstream_index] + '</td>\n'
	
	return result

def get_comments(package_names: [str]) -> ({str : str}, {str : {str : str}}):
	request = requests.get(
		"https://pagure.io/java-pkg-versions-comments/raw/master/f/comments.md",
		timeout = 7)
	
	if request.status_code != 200:
		raise RuntimeError("Could not obtain the comments")
	
	result = {package_name: "" for package_name in package_names}
	tags = {package_name: "" for package_name in package_names}
	name = str()
	comment = str()

	for line in request.text.splitlines():
		# A new package name
		if line.startswith("#") and not line.startswith("##"):
			name = line[1:].strip()
			tags[name] = dict()
		
		# A new tag
		elif line.startswith("##"):
			if name:
				match = re.match("##\\s*(.*?)\\s*:\\s*(.*)", line)
				tags[name][match.group(1)] = match.group(2).rstrip()
		
		# End of the comment for the current package name
		elif line.startswith("---") or (line.startswith("#") and not line.startswith("##") and name):
			if name:
				match = re.match("<p>(.*)</p>\\s*", markdown2.markdown(comment), re.DOTALL)
				result[name] = match.group(1)
			name = str()
			comment = str()
		
		elif name:
			comment += line
	
	return result, tags

################################################################################

# Tests

assert(normalize_version("") == "")
assert(normalize_version("1.0b3") == "1.0~b3")
assert(normalize_version("2.5.0-rc1") == "2.5.0~rc1")
assert(normalize_version("2.0b6") == "2.0~b6")
assert(normalize_version("2.0.SP1") == "2.0.SP1")
assert(normalize_version("3_2_12") == "3.2.12")
assert(normalize_version("1.0-20050927.133100") == "1.0.20050927.133100")
assert(normalize_version("3.0.1-b11") == "3.0.1~b11")
assert(normalize_version("5.0.1-b04") == "5.0.1~b04")
assert(normalize_version("0.11b") == "0.11b")
assert(normalize_version("1_6_2") == "1.6.2")
assert(normalize_version("1.0.1.Final") == "1.0.1")
assert(normalize_version("3.0.0.M1") == "3.0.0~M1")
assert(normalize_version("6.0-alpha-2") == "6.0~alpha.2")
assert(normalize_version("4.13-beta-1") == "4.13~beta.1")
assert(normalize_version("5.5.0-M1") == "5.5.0~M1")
assert(normalize_version("3.0.0-M2") == "3.0.0~M2")
assert(normalize_version("3.0.0-M1") == "3.0.0~M1")
assert(normalize_version("3.0.0-M3") == "3.0.0~M3")
assert(normalize_version("3.0.0-beta.1") == "3.0.0~beta.1")
assert(normalize_version("1.0-alpha-2.1") == "1.0~alpha.2.1")
assert(normalize_version("1.0-alpha-8") == "1.0~alpha.8")
assert(normalize_version("1.0-alpha-18") == "1.0~alpha.18")
assert(normalize_version("1.0-alpha-10") == "1.0~alpha.10")
assert(normalize_version("1.0-beta-7") == "1.0~beta.7")
assert(normalize_version("1.0-alpha-5") == "1.0~alpha.5")
assert(normalize_version("2.0-M10") == "2.0~M10")
assert(normalize_version("7.0.0-beta4") == "7.0.0~beta4")

################################################################################

# Main function

versions_all = get_all_versions()
comments_all, tags_all = get_comments(versions_all.keys())

with open("versions.html", "w") as table:
	table.write('<link rel=stylesheet href=mystyle.css>')
	table.write('<table>\n')
	
	table.write('<th>' + 'Package name' + '</th>')
	
	for header_name in releases:
		table.write('<th>' + header_name + '</th>')
	
	table.write('<th>' + 'Comment' + '</th>')
	
	table.write('<th>' + 'Links' + '</th>')
	
	for pkg_name, version_list in versions_all.items():
		table.write('<tr>\n')
		
		# Package name
		table.write('<td>' + pkg_name + '</td>\n')
		
		# Versions
		table.write(row_to_str(version_list, tags_all[pkg_name]))
		
		# Comment
		table.write('<td>\n')
		table.write(comments_all[pkg_name])
		table.write('</td>\n')
		
		# Links
		table.write('<td>\n')
		table.write('MBI\n')
		table.write('(<a href="https://src.fedoraproject.org/fork/mbi/rpms/' + pkg_name + '" target="_blank">dist-git</a>)\n')
		table.write('(<a href="https://koji.kjnet.xyz/koji/packageinfo?packageID=' + pkg_name + '" target="_blank">Koji</a>)\n')
		table.write('(<a href="https://koschei.kjnet.xyz/koschei/package/' + pkg_name + '?collection=jp" target="_blank">Koschei</a>)\n')
		table.write('</td>\n')
		
		table.write('</tr>\n')
	table.write('</table>\n')
