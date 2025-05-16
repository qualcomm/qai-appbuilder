#include <jni.h>
#include <string>

extern "C" JNIEXPORT jstring JNICALL
Java_com_example_genieapiservice_MainActivity_stringFromJNI(
        JNIEnv* env,
        jobject /* this */) {
    std::string hello = "<br><br><br><br><br><br><br><br><br><br><h1>Initializing Genie Service...</h1>";
    return env->NewStringUTF(hello.c_str());
}
