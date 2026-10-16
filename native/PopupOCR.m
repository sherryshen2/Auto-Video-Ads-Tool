#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

// Reads locally captured frames only; no network services or model downloads.
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSMutableArray *output = [NSMutableArray array];
        for (int i = 1; i < argc; i++) {
            @autoreleasepool {
                NSString *path = [NSString stringWithUTF8String:argv[i]];
                VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
                request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
                request.recognitionLanguages = @[@"en-US"];
                request.usesLanguageCorrection = YES;
                request.minimumTextHeight = 0.008;
                VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
                    initWithURL:[NSURL fileURLWithPath:path] options:@{}];
                NSError *error = nil;
                if (![handler performRequests:@[request] error:&error]) {
                    [output addObject:@{@"file": path.lastPathComponent,
                        @"error": error.localizedDescription ?: @"OCR failed"}];
                    continue;
                }
                NSMutableArray *lines = [NSMutableArray array];
                for (VNRecognizedTextObservation *observation in request.results) {
                    VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
                    if (candidate && candidate.confidence >= 0.25) [lines addObject:candidate.string];
                }
                [output addObject:@{@"file": path.lastPathComponent, @"lines": lines}];
            }
        }
        NSData *data = [NSJSONSerialization dataWithJSONObject:output options:NSJSONWritingSortedKeys error:nil];
        if (!data) return 1;
        [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
    }
    return 0;
}
