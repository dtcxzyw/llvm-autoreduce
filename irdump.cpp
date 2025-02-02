// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2025 Yingwei Zheng
// This file is licensed under the Apache-2.0 License.
// See the LICENSE file for more information.

#include <llvm/IR/Function.h>
#include <llvm/IR/Module.h>
#include <llvm/Pass.h>
#include <llvm/Passes/PassBuilder.h>
#include <llvm/Passes/PassPlugin.h>
#include <llvm/Support/JSON.h>

using namespace llvm;

class Logger {
  json::Array Root;

public:
  void record(StringRef ClassName, StringRef PassName, const Module &M) {
    json::Object Rec;
    Rec["class"] = ClassName;
    if (!PassName.data())
      Rec["pass"] = PassName;
    std::string Out;
    {
      raw_string_ostream OS(Out);
      M.print(OS, nullptr);
    }
    Rec["module"] = std::move(Out);
    Root.push_back(std::move(Rec));
  }
  ~Logger() {
    errs() << "```json\n";
    errs() << json::Value(std::move(Root)) << '\n';
    errs() << "```\n";
  }
  static Logger &get() {
    static Logger Instance;
    return Instance;
  }
};

template <typename IRUnitT> static const IRUnitT *unwrapIR(Any IR) {
  const IRUnitT **IRPtr = llvm::any_cast<const IRUnitT *>(&IR);
  return IRPtr ? *IRPtr : nullptr;
}

/// Extract Module out of \p IR unit. May return nullptr if \p IR does not
/// match certain global filters. Will never return nullptr if \p Force is
/// true.
const Module *unwrapModule(Any IR) {
  if (const auto *M = unwrapIR<Module>(IR))
    return M;

  if (const auto *F = unwrapIR<Function>(IR))
    return F->getParent();

  if (const auto *C = unwrapIR<LazyCallGraph::SCC>(IR)) {
    for (const LazyCallGraph::Node &N : *C) {
      const Function &F = N.getFunction();
      return F.getParent();
    }
    return nullptr;
  }

  if (const auto *L = unwrapIR<Loop>(IR)) {
    const Function *F = L->getHeader()->getParent();
    return F->getParent();
  }

  llvm_unreachable("Unknown IR unit");
}

static PassPluginLibraryInfo getDumpPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "irdump", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            auto &PIC = *PB.getPassInstrumentationCallbacks();
            PIC.registerAfterPassCallback(
                [&](StringRef P, Any IR, const PreservedAnalyses &) {
                  Logger::get().record(P, PIC.getPassNameForClassName(P),
                                       *unwrapModule(IR));
                });
          }};
}

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return getDumpPluginInfo();
}
