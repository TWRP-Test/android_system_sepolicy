// Copyright 2021 The Android Open Source Project
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package selinux

import (
	"fmt"

	"github.com/google/blueprint/proptools"

	"android/soong/android"
)

var (
	compatTestDepTag = dependencyTag{name: "compat_test"}
)

func init() {
	ctx := android.InitRegistrationContext
	ctx.RegisterModuleType("se_compat_cil", compatCilFactory)
	ctx.RegisterModuleType("se_compat_test", compatTestFactory)
}

// se_compat_cil collects and installs backwards compatibility cil files.
func compatCilFactory() android.Module {
	c := &compatCil{}
	c.AddProperties(&c.properties)
	android.InitAndroidArchModule(c, android.DeviceSupported, android.MultilibCommon)
	return c
}

type compatCil struct {
	android.ModuleBase
	properties    compatCilProperties
	installSource android.OptionalPath
	installPath   android.InstallPath
}

type compatCilProperties struct {
	// List of source files. Can reference se_build_files type modules with the ":module" syntax.
	Srcs []string `android:"path"`

	// Output file name. Defaults to module name if unspecified.
	Stem *string

	// Target version that this module supports. This module will be ignored if platform sepolicy
	// version is same as this module's version.
	Version *string
}

func (c *compatCil) stem() string {
	return proptools.StringDefault(c.properties.Stem, c.Name())
}

func (c *compatCil) expandSeSources(ctx android.ModuleContext) android.Paths {
	return android.PathsForModuleSrc(ctx, c.properties.Srcs)
}

func (c *compatCil) shouldSkipBuild(ctx android.ModuleContext) bool {
	return proptools.String(c.properties.Version) == ctx.DeviceConfig().PlatformSepolicyVersion()
}

func (c *compatCil) GenerateAndroidBuildActions(ctx android.ModuleContext) {
	if c.ProductSpecific() || c.SocSpecific() || c.DeviceSpecific() {
		ctx.ModuleErrorf("Compat cil files only support system and system_ext partitions")
	}

	if c.shouldSkipBuild(ctx) {
		return
	}

	srcPaths := c.expandSeSources(ctx)
	out := android.PathForModuleGen(ctx, c.Name())
	ctx.Build(pctx, android.BuildParams{
		Rule:        android.Cat,
		Inputs:      srcPaths,
		Output:      out,
		Description: "Combining compat cil for " + c.Name(),
	})

	c.installPath = android.PathForModuleInstall(ctx, "etc", "selinux", "mapping")
	c.installSource = android.OptionalPathForPath(out)
	ctx.InstallFile(c.installPath, c.stem(), out)

	if c.installSource.Valid() {
		ctx.SetOutputFiles(android.Paths{c.installSource.Path()}, "")
	}
}

func (c *compatCil) AndroidMkEntries() []android.AndroidMkEntries {
	if !c.installSource.Valid() {
		return nil
	}
	return []android.AndroidMkEntries{android.AndroidMkEntries{
		Class:      "ETC",
		OutputFile: c.installSource,
		ExtraEntries: []android.AndroidMkExtraEntriesFunc{
			func(ctx android.AndroidMkExtraEntriesContext, entries *android.AndroidMkEntries) {
				entries.SetPath("LOCAL_MODULE_PATH", c.installPath)
				entries.SetString("LOCAL_INSTALLED_MODULE_STEM", c.stem())
			},
		},
	}}
}

// se_compat_test checks if compat files ({ver}.cil, {ver}.compat.cil) files are compatible with
// current policy.
func compatTestFactory() android.Module {
	f := &compatTestModule{}
	f.AddProperties(&f.properties)
	android.InitAndroidArchModule(f, android.DeviceSupported, android.MultilibCommon)
	android.AddLoadHook(f, func(ctx android.LoadHookContext) {
		f.loadHook(ctx)
	})
	return f
}

type compatTestModule struct {
	android.ModuleBase
	properties struct {
		// Default modules for conf
		Defaults []string
	}

	compatTestTimestamp android.ModuleOutPath
}

func (f *compatTestModule) createCompatTestModule(ctx android.LoadHookContext, ver string) {
	srcs := []string{
		":plat_sepolicy.cil",
		":system_ext_sepolicy.cil",
		":product_sepolicy.cil",
		fmt.Sprintf(":plat_%s.cil", ver),
		fmt.Sprintf(":%s.compat.cil", ver),
		fmt.Sprintf(":system_ext_%s.cil", ver),
		fmt.Sprintf(":system_ext_%s.compat.cil", ver),
		fmt.Sprintf(":product_%s.cil", ver),
	}

	if ver == ctx.DeviceConfig().BoardSepolicyVers() {
		srcs = append(srcs,
			":plat_pub_versioned.cil",
			":vendor_sepolicy.cil",
			":odm_sepolicy.cil",
		)
	} else {
		srcs = append(srcs, fmt.Sprintf(":%s_plat_pub_versioned.cil", ver))
	}

	compatTestName := fmt.Sprintf("%s_compat_test", ver)
	ctx.CreateModule(policyBinaryFactory, &nameProperties{
		Name: proptools.StringPtr(compatTestName),
	}, &policyBinaryProperties{
		Srcs:              srcs,
		Ignore_neverallow: proptools.BoolPtr(true),
		Installable:       proptools.BoolPtr(false),
	})
}

func (f *compatTestModule) loadHook(ctx android.LoadHookContext) {
	for _, ver := range ctx.DeviceConfig().PlatformSepolicyCompatVersions() {
		f.createCompatTestModule(ctx, ver)
	}
}

func (f *compatTestModule) DepsMutator(ctx android.BottomUpMutatorContext) {
	for _, ver := range ctx.DeviceConfig().PlatformSepolicyCompatVersions() {
		ctx.AddDependency(f, compatTestDepTag, fmt.Sprintf("%s_compat_test", ver))
	}
}

func (f *compatTestModule) GenerateAndroidBuildActions(ctx android.ModuleContext) {
	if ctx.ModuleName() != "sepolicy_compat_test" || ctx.ModuleDir() != "system/sepolicy/compat" {
		// two compat test modules don't make sense.
		ctx.ModuleErrorf("There can only be 1 se_compat_test module named sepolicy_compat_test in system/sepolicy/compat")
	}
	var inputs android.Paths
	ctx.VisitDirectDepsWithTag(compatTestDepTag, func(child android.Module) {
		outputs := android.OutputFilesForModule(ctx, child, "")
		if len(outputs) != 1 {
			panic(fmt.Errorf("Module %q should produce exactly one output, but did %q", ctx.OtherModuleName(child), outputs.Strings()))
		}

		inputs = append(inputs, outputs[0])
	})

	f.compatTestTimestamp = android.PathForModuleOut(ctx, "timestamp")
	rule := android.NewRuleBuilder(pctx, ctx)
	rule.Command().Text("touch").Output(f.compatTestTimestamp).Implicits(inputs)
	rule.Build("compat", "compat test timestamp for: "+f.Name())
}

func (f *compatTestModule) AndroidMkEntries() []android.AndroidMkEntries {
	return []android.AndroidMkEntries{android.AndroidMkEntries{
		Class: "FAKE",
		// OutputFile is needed, even though BUILD_PHONY_PACKAGE doesn't use it.
		// Without OutputFile this module won't be exported to Makefile.
		OutputFile: android.OptionalPathForPath(f.compatTestTimestamp),
		Include:    "$(BUILD_PHONY_PACKAGE)",
		ExtraEntries: []android.AndroidMkExtraEntriesFunc{
			func(ctx android.AndroidMkExtraEntriesContext, entries *android.AndroidMkEntries) {
				entries.SetString("LOCAL_ADDITIONAL_DEPENDENCIES", f.compatTestTimestamp.String())
			},
		},
	}}
}
