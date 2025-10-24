#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
# 
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

import os
import sys
import importlib.util

spec = importlib.util.find_spec("qai_appbuilder.geniebuilder")
if spec is not None:
    from qai_appbuilder import geniebuilder
# else:
#    print("geniebuilder is not exist.")

class GenieContext:
    """High-level Python wrapper for a GenieBuilder model."""
    def __init__(self,
                config: str = "None",
                debug: bool = False
    ) -> None:
        self.config = config
        self.debug = debug
        self.m_context = geniebuilder.GenieContext(config, debug)

    def Query(self, prompt, callback):
        return self.m_context.Query(prompt, callback)

    def Stop(self):
        return self.m_context.Stop()

    def SetParams(self, max_length, temp, top_k, top_p):
        return self.m_context.SetParams(max_length, temp, top_k, top_p)

    def SetStopSequence(self, stop_sequences):
        return self.m_context.SetStopSequence(stop_sequences)

    def GetProfile(self):
        return self.m_context.GetProfile()

    def SetLora(self, adapter_name, alpha_value):
        return self.m_context.SetLora(adapter_name, alpha_value)

    def TokenLength(self, text):
        return self.m_context.TokenLength(text)

    def __del__(self):
        if hasattr(self, "m_context") and self.m_context is not None:
            del(self.m_context)
            m_context = None
