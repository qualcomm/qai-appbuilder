if (NOT LLAMACPP_SRC_DIR OR NOT EXISTS "${LLAMACPP_SRC_DIR}/common/fit.cpp")
    message(FATAL_ERROR "llama_fit_user_overrides.cmake: not a llama.cpp source tree: ${LLAMACPP_SRC_DIR}")
endif ()

set(_fit "${LLAMACPP_SRC_DIR}/common/fit.cpp")
file(READ "${_fit}" _src)
string(FIND "${_src}" "n_user_tbo" _done)
if (NOT _done EQUAL -1)
    message(STATUS "llama_fit_user_overrides: already patched")
    return()
endif ()

function(_fit_replace _from _to)
    string(FIND "${_src}" "${_from}" _pos)
    if (_pos EQUAL -1)
        message(FATAL_ERROR "llama_fit_user_overrides: anchor not found in ${_fit}: ${_from}")
    endif ()
    string(REPLACE "${_from}" "${_to}" _out "${_src}")
    set(_src "${_out}" PARENT_SCOPE)
endfunction()

_fit_replace([=[throw common_params_fit_exception("model_params::tensor_buft_overrides already set by user, abort");]=]
        [=[(void) 0;]=])
_fit_replace([=[    const size_t ntbo = llama_max_tensor_buft_overrides();]=]
        [=[    const size_t ntbo = llama_max_tensor_buft_overrides();
    size_t n_user_tbo = 0;
    while (n_user_tbo < ntbo && tensor_buft_overrides[n_user_tbo].pattern != nullptr) {
        n_user_tbo++;
    }]=])
_fit_replace([=[size_t itbo = 0;]=] [=[size_t itbo = n_user_tbo;]=])
_fit_replace([=[tensor_buft_overrides[0] = {pattern_moe_all.c_str(), cpu_buft};]=]
        [=[tensor_buft_overrides[n_user_tbo] = {pattern_moe_all.c_str(), cpu_buft};]=])
_fit_replace([=[tensor_buft_overrides[1] = {nullptr, nullptr};]=]
        [=[tensor_buft_overrides[n_user_tbo + 1] = {nullptr, nullptr};]=])
_fit_replace([=[tensor_buft_overrides[0] = {nullptr, nullptr};]=]
        [=[tensor_buft_overrides[n_user_tbo] = {nullptr, nullptr};]=])

file(WRITE "${_fit}" "${_src}")
message(STATUS "llama_fit_user_overrides: patched ${_fit}")
