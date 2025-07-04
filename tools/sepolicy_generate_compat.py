#!/usr/bin/env python3

# Copyright 2022 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
import argparse
import glob
import logging
import mini_parser
import os
import pkgutil
import policy
import shutil
import subprocess
import sys
import tempfile
import zipfile
"""This tool generates a mapping file for {ver} core sepolicy."""

temp_dir = ''
mapping_cil_footer = ";; mapping information from ToT policy's types to %s policy's types.\n"
compat_cil_template = """;; complement CIL file for compatibility between ToT policy and %s vendors.
;; will be compiled along with other normal policy files, on %s vendors.
;;
"""
ignore_cil_template = """;; new_objects - a collection of types that have been introduced with ToT policy
;;   that have no analogue in %s policy.  Thus, we do not need to map these types to
;;   previous ones.  Add here to pass checkapi tests.
(type new_objects)
(typeattribute new_objects)
(typeattributeset new_objects
  ( new_objects
    %s
  ))
"""

SHARED_LIB_EXTENSION = '.dylib' if sys.platform == 'darwin' else '.so'

def check_run(cmd, cwd=None):
    if cwd:
        logging.debug('Running cmd at %s: %s' % (cwd, cmd))
    else:
        logging.debug('Running cmd: %s' % cmd)
    subprocess.run(cmd, cwd=cwd, check=True)


def check_output(cmd):
    logging.debug('Running cmd: %s' % cmd)
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE)


def get_android_build_top():
    ANDROID_BUILD_TOP = os.getenv('ANDROID_BUILD_TOP')
    if not ANDROID_BUILD_TOP:
        sys.exit(
            'Error: Missing ANDROID_BUILD_TOP env variable. Please run '
            '\'. build/envsetup.sh; lunch <build target>\'. Exiting script.')
    return ANDROID_BUILD_TOP


def fetch_artifact(branch, build, pattern, destination='.'):
    """Fetches build artifacts from Android Build server.

    Args:
      branch: string, branch to pull build artifacts from
      build: string, build ID or "latest"
      pattern: string, pattern of build artifact file name
      destination: string, destination to pull build artifact to
    """
    fetch_artifact_path = '/google/data/ro/projects/android/fetch_artifact'
    cmd = [
        fetch_artifact_path, '--branch', branch, '--target',
        'aosp_arm64-userdebug'
    ]
    if build == 'latest':
        cmd.append('--latest')
    else:
        cmd.extend(['--bid', build])
    cmd.extend([pattern, destination])
    check_run(cmd)


def extract_mapping_file_from_img_zip(zip_path, ver, destination='.'):
    """ Extracts system/etc/selinux/mapping/{ver}.cil from img.zip file.

    Args:
      zip_path: string, path to img.zip file
      ver: string, version of designated mapping file
      destination: string, destination to pull the mapping file to

    Returns:
      string, path to extracted mapping file
    """
    with zipfile.ZipFile(zip_path) as zip_file:
        logging.debug(f'Extracting system.img to {temp_dir}')
        zip_file.extract('system.img', temp_dir)

    system_img_path = os.path.join(temp_dir, 'system.img')
    mapping_file_path = os.path.join(destination, f'{ver}.cil')
    cmd = [
        'debugfs', '-R',
        f'dump system/etc/selinux/mapping/{ver}.cil {mapping_file_path}',
        system_img_path
    ]
    logging.debug(f'Extracting {ver}.cil to {destination}')
    check_run(cmd)
    return mapping_file_path


def download_img_zip(branch, build, destination='.'):
    """ Downloads system/etc/selinux/mapping/{ver}.cil from Android Build server.

    Args:
      branch: string, branch to pull build artifacts from (e.g. "sc-v2-dev")
      build: string, build ID or "latest"
      ver: string, version of designated mapping file (e.g. "32.0")
      destination: string, destination to pull build artifact to

    Returns:
      string, path to img.zip file
    """
    logging.info('Downloading %s mapping file from branch %s build %s...' %
                 (ver, branch, build))
    artifact_pattern = 'aosp_arm64-img-*.zip'
    fetch_artifact(branch, build, artifact_pattern, temp_dir)

    # glob must succeed
    return glob.glob(os.path.join(temp_dir, artifact_pattern))[0]


def build_base_files(target_version):
    """ Builds needed base policy files from the source code.

    Args:
      target_version: string, target version to gerenate the mapping file

    Returns:
      (string, string, string), paths to base policy, old policy, and pub policy
      cil
    """
    logging.info('building base sepolicy files')
    build_top = get_android_build_top()

    cmd = [
        'build/soong/soong_ui.bash',
        '--make-mode',
        'dist',
        'base-sepolicy-files-for-mapping',
        'TARGET_PRODUCT=aosp_arm64',
        'TARGET_BUILD_VARIANT=userdebug',
    ]
    check_run(cmd, cwd=build_top)

    dist_dir = os.path.join(build_top, 'out', 'dist')
    base_policy_path = os.path.join(dist_dir, 'base_plat_sepolicy')
    old_policy_path = os.path.join(dist_dir,
                                   '%s_plat_policy' % target_version)
    pub_policy_cil_path = os.path.join(dist_dir, 'base_plat_pub_policy.cil')

    return base_policy_path, old_policy_path, pub_policy_cil_path


def change_api_level(versioned_type, api_from, api_to):
    """ Verifies the API version of versioned_type, and changes it to new API level.

    For example, change_api_level("foo_202404", "202404", "202504") will return
    "foo_202504".

    Args:
      versioned_type: string, type with version suffix
      api_from: string, api version of versioned_type
      api_to: string, new api version for versioned_type

    Returns:
      string, a new versioned type
    """
    if not versioned_type.endswith(api_from):
        raise ValueError('Version of type %s is different from %s' %
                         (versioned_type, api_from))
    return versioned_type.removesuffix(api_from) + api_to


def create_target_compat_modules(bp_path, target_ver):
    """ Creates compat modules to Android.bp.

    Args:
      bp_path: string, path to Android.bp
      target_ver: string, api version to generate
    """

    module_template = """
se_build_files {{
    name: "{ver}.board.compat.map",
    srcs: ["compat/{ver}/{ver}.cil"],
}}

se_build_files {{
    name: "{ver}.board.compat.cil",
    srcs: ["compat/{ver}/{ver}.compat.cil"],
}}

se_build_files {{
    name: "{ver}.board.ignore.map",
    srcs: ["compat/{ver}/{ver}.ignore.cil"],
}}

se_cil_compat_map {{
    name: "plat_{ver}.cil",
    stem: "{ver}.cil",
    bottom_half: [":{ver}.board.compat.map{{.plat_private}}"],
    version: "{ver}",
}}

se_cil_compat_map {{
    name: "system_ext_{ver}.cil",
    stem: "{ver}.cil",
    bottom_half: [":{ver}.board.compat.map{{.system_ext_private}}"],
    system_ext_specific: true,
    version: "{ver}",
}}

se_cil_compat_map {{
    name: "product_{ver}.cil",
    stem: "{ver}.cil",
    bottom_half: [":{ver}.board.compat.map{{.product_private}}"],
    product_specific: true,
    version: "{ver}",
}}

se_cil_compat_map {{
    name: "{ver}.ignore.cil",
    bottom_half: [":{ver}.board.ignore.map{{.plat_private}}"],
    version: "{ver}",
}}

se_cil_compat_map {{
    name: "system_ext_{ver}.ignore.cil",
    stem: "{ver}.ignore.cil",
    bottom_half: [":{ver}.board.ignore.map{{.system_ext_private}}"],
    system_ext_specific: true,
    version: "{ver}",
}}

se_cil_compat_map {{
    name: "product_{ver}.ignore.cil",
    stem: "{ver}.ignore.cil",
    bottom_half: [":{ver}.board.ignore.map{{.product_private}}"],
    product_specific: true,
    version: "{ver}",
}}

se_compat_cil {{
    name: "{ver}.compat.cil",
    srcs: [":{ver}.board.compat.cil{{.plat_private}}"],
    version: "{ver}",
}}

se_compat_cil {{
    name: "system_ext_{ver}.compat.cil",
    stem: "{ver}.compat.cil",
    srcs: [":{ver}.board.compat.cil{{.system_ext_private}}"],
    system_ext_specific: true,
    version: "{ver}",
}}
"""

    with open(bp_path, 'a') as f:
        f.write(module_template.format(ver=target_ver))


def patch_top_half_of_latest_compat_modules(bp_path, latest_ver, target_ver):
    """ Adds top_half property to latest compat modules in Android.bp.

    Args:
      bp_path: string, path to Android.bp
      latest_ver: string, previous api version
      target_ver: string, api version to generate
    """

    modules_to_patch = [
        "plat_{ver}.cil",
        "system_ext_{ver}.cil",
        "product_{ver}.cil",
        "{ver}.ignore.cil",
        "system_ext_{ver}.ignore.cil",
        "product_{ver}.ignore.cil",
    ]

    for module in modules_to_patch:
        # set latest_ver module's top_half property to target_ver
        # e.g.
        #
        # se_cil_compat_map {
        #    name: "plat_33.0.cil",
        #    top_half: "plat_34.0.cil", <== this
        #    ...
        # }
        check_run([
            "bpmodify",
            "-m", module.format(ver=latest_ver),
            "-property", "top_half",
            "-str", module.format(ver=target_ver),
            "-w",
            bp_path
        ])

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--branch',
        help='Branch to pull build from. e.g. "sc-v2-dev"')
    parser.add_argument('--build',
        default='latest',
        help='Build ID, or "latest"')
    parser.add_argument(
        '--target-version',
        required=True,
        help='Target version of designated mapping file. e.g. "202504"')
    parser.add_argument(
        '--latest-version',
        required=True,
        help='Latest version for mapping of newer types. e.g. "202404"')
    parser.add_argument(
        '--img-zip',
        help='Pre-downloaded img.zip. e.g. "aosp_arm64-img-xxxxxxxx.zip"')
    parser.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='Increase output verbosity, e.g. "-v", "-vv".')
    return parser.parse_args()


def main():
    args = get_args()

    verbosity = min(args.verbose, 2)
    logging.basicConfig(
        format='%(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[verbosity])

    global temp_dir
    temp_dir = tempfile.mkdtemp()

    try:
        libname = "libsepolwrap" + SHARED_LIB_EXTENSION
        libpath = os.path.join(temp_dir, libname)
        with open(libpath, "wb") as f:
            blob = pkgutil.get_data("sepolicy_generate_compat", libname)
            if not blob:
                sys.exit("Error: libsepolwrap does not exist. Is this binary corrupted?\n")
            f.write(blob)

        build_top = get_android_build_top()
        sepolicy_path = os.path.join(build_top, 'system', 'sepolicy')

        # Step 0. Create a placeholder files and compat modules
        # These are needed to build base policy files below.
        compat_bp_path = os.path.join(sepolicy_path, 'compat', 'Android.bp')
        create_target_compat_modules(compat_bp_path, args.target_version)
        patch_top_half_of_latest_compat_modules(compat_bp_path, args.latest_version,
            args.target_version)

        target_compat_path = os.path.join(sepolicy_path, 'private', 'compat',
                                          args.target_version)
        target_mapping_file = os.path.join(target_compat_path,
                                           args.target_version + '.cil')
        target_compat_file = os.path.join(target_compat_path,
                                          args.target_version + '.compat.cil')
        target_ignore_file = os.path.join(target_compat_path,
                                          args.target_version + '.ignore.cil')
        Path(target_compat_path).mkdir(parents=True, exist_ok=True)
        Path(target_mapping_file).touch()
        Path(target_compat_file).touch()
        Path(target_ignore_file).touch()

        # Step 1. Download system/etc/selinux/mapping/{ver}.cil, and remove types/typeattributes
        if args.img_zip and args.branch:
            sys.exit('Error: only one of --img-zip and --branch can be set')
        elif args.img_zip:
            img_zip = args.img_zip
        elif args.branch:
            img_zip = download_img_zip(args.branch, args.build, destination=temp_dir)
        else:
            sys.exit('Error: either one of --img-zip and --branch must be set')
        mapping_file = extract_mapping_file_from_img_zip(img_zip, args.target_version,
                                                         destination=temp_dir)
        mapping_file_cil = mini_parser.MiniCilParser(mapping_file)
        mapping_file_cil.types = set()
        mapping_file_cil.typeattributes = set()

        # Step 2. Build base policy files and parse latest mapping files
        base_policy_path, old_policy_path, pub_policy_cil_path = build_base_files(
            args.target_version)
        base_policy = policy.Policy(base_policy_path, None, libpath)
        old_policy = policy.Policy(old_policy_path, None, libpath)
        pub_policy_cil = mini_parser.MiniCilParser(pub_policy_cil_path)

        all_types = base_policy.GetAllTypes(False)
        old_all_types = old_policy.GetAllTypes(False)
        pub_types = pub_policy_cil.types

        # Step 3. Find new types and removed types
        new_types = pub_types & (all_types - old_all_types)
        removed_types = (mapping_file_cil.pubtypes - mapping_file_cil.types) & (
            old_all_types - all_types)

        logging.info('new types: %s' % new_types)
        logging.info('removed types: %s' % removed_types)

        # Step 4. Map new types and removed types appropriately, based on the latest mapping
        latest_compat_path = os.path.join(sepolicy_path, 'private', 'compat',
                                          args.latest_version)
        latest_mapping_cil = mini_parser.MiniCilParser(
            os.path.join(latest_compat_path, args.latest_version + '.cil'))
        latest_ignore_cil = mini_parser.MiniCilParser(
            os.path.join(latest_compat_path,
                         args.latest_version + '.ignore.cil'))

        latest_ignored_types = list(latest_ignore_cil.rTypeattributesets.keys())
        latest_removed_types = latest_mapping_cil.types
        logging.debug('types ignored in latest policy: %s' %
                      latest_ignored_types)
        logging.debug('types removed in latest policy: %s' %
                      latest_removed_types)

        target_ignored_types = set()
        target_removed_types = set()
        invalid_new_types = set()
        invalid_mapping_types = set()
        invalid_removed_types = set()

        logging.info('starting mapping')
        for new_type in new_types:
            # Either each new type should be in latest_ignore_cil, or mapped to existing types
            if new_type in latest_ignored_types:
                logging.debug('adding %s to ignore' % new_type)
                target_ignored_types.add(new_type)
            elif new_type in latest_mapping_cil.rTypeattributesets:
                latest_mapped_types = latest_mapping_cil.rTypeattributesets[
                    new_type]
                target_mapped_types = {change_api_level(t, args.latest_version,
                                        args.target_version)
                       for t in latest_mapped_types}
                logging.debug('mapping %s to %s' %
                              (new_type, target_mapped_types))

                for t in target_mapped_types:
                    if t not in mapping_file_cil.typeattributesets:
                        logging.error(
                            'Cannot find desired type %s in mapping file' % t)
                        invalid_mapping_types.add(t)
                        continue
                    mapping_file_cil.typeattributesets[t].add(new_type)
            else:
                logging.error('no mapping information for new type %s' %
                              new_type)
                invalid_new_types.add(new_type)

        for removed_type in removed_types:
            # Removed type should be in latest_mapping_cil
            if removed_type in latest_removed_types:
                logging.debug('adding %s to removed' % removed_type)
                target_removed_types.add(removed_type)
            else:
                logging.error('no mapping information for removed type %s' %
                              removed_type)
                invalid_removed_types.add(removed_type)

        error_msg = ''

        if invalid_new_types:
            error_msg += ('The following new types were not in the latest '
                          'mapping: %s\n') % sorted(invalid_new_types)
        if invalid_mapping_types:
            error_msg += (
                'The following existing types were not in the '
                'downloaded mapping file: %s\n') % sorted(invalid_mapping_types)
        if invalid_removed_types:
            error_msg += ('The following removed types were not in the latest '
                          'mapping: %s\n') % sorted(invalid_removed_types)

        if error_msg:
            error_msg += '\n'
            error_msg += ('Please make sure the source tree and the build ID is'
                          ' up to date.\n')
            sys.exit(error_msg)

        # Step 5. Write to system/sepolicy/private/compat
        with open(target_mapping_file, 'w') as f:
            logging.info('writing %s' % target_mapping_file)
            if removed_types:
                f.write(';; types removed from current policy\n')
                f.write('\n'.join(f'(type {x})' for x in sorted(target_removed_types)))
                f.write('\n\n')
            f.write(mapping_cil_footer % args.target_version)
            f.write(mapping_file_cil.unparse())

        with open(target_compat_file, 'w') as f:
            logging.info('writing %s' % target_compat_file)
            f.write(compat_cil_template % (args.target_version, args.target_version))

        with open(target_ignore_file, 'w') as f:
            logging.info('writing %s' % target_ignore_file)
            f.write(ignore_cil_template %
                    (args.target_version, '\n    '.join(sorted(target_ignored_types))))

        # TODO(b/391513934): add treble tests
        # TODO(b/391513934): add mapping files to phony modules like selinux_policy_system
    finally:
        logging.info('Deleting temporary dir: {}'.format(temp_dir))
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    main()
