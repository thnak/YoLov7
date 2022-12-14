import argparse
import time
from pathlib import Path
import cv2
import torch
import torch.backends.cudnn as cudnn
from termcolor import colored
from models.experimental import attempt_load
from models.yolo import TensorRT_Engine
from utils.datasets import LoadStreams, LoadImages, is_stream_or_webcam
from utils.general import check_img_size, check_requirements, non_max_suppression, \
    scale_coords, xyxy2xywh, strip_optimizer, set_logging, increment_path, check_git_status, BackgroundForegroundColors, colorstr
from utils.plots import plot_one_box_with_return
from utils.torch_utils import select_device, time_synchronized, TracedModel
import os
import numpy as np
import threading

from utils.ffmpeg_ import  FFMPEG_recorder

def detect(opt=None):
    source, weights, view_img, save_txt, imgsz, trace = opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size, not opt.no_trace
    save_img = not opt.nosave and not source.endswith('.txt')  # save inference images
    webcam = is_stream_or_webcam(source)

    # Directories
    save_dir = Path(increment_path(Path(opt.project) / opt.name,
                    exist_ok=opt.exist_ok))  # increment run
    (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir
    
    # Initialize
    set_logging()
    device = select_device(opt.device)[0]
    half = device.type == 'cuda'  # half precision only supported on CUDA

    # Load model
    model = attempt_load(weights, map_location='cpu').to(device)  # load FP32 model
    stride = int(model.stride.max())  # model stride
    imgsz = check_img_size(imgsz, s=stride)  # check img_size

    if trace:
        model = TracedModel(model=model, device=device,
                            img_size=imgsz, saveTrace=False)

    if half:
        model.half()  # to FP16


    vid_path = None
    if webcam:
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=imgsz, stride=stride, auto=True)
    else:
        dataset = LoadImages(source, img_size=imgsz, stride=stride, auto=True)

    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names
    BFC = BackgroundForegroundColors(names=names)
    # Run inference
    if device.type == 'cuda':
        model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))  # run once
    old_img_w = old_img_h = imgsz
    old_img_b = 1

    t0 = time.time()
    avgTime = [[], []]
    if opt.datacollection:
        save_txt = True
    for path, img, im0s, vid_cap, s, ratio, dwdh in dataset:
        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()
        img /= 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        # Warmup
        if device.type == 'cuda' and (old_img_b != img.shape[0] or old_img_h != img.shape[2] or old_img_w != img.shape[3]):
            twrm = time.time()
            old_img_b = img.shape[0]
            old_img_h = img.shape[2]
            old_img_w = img.shape[3]
            for i in range(3):
                model(img, augment=opt.augment)[0]
            print(f'{time.time() - twrm:0.3f} warm up finished')
        # Inference
        t1 = time_synchronized()
        
        pred = model(img, augment=opt.augment)[0]
        t2 = time_synchronized()

        pred = non_max_suppression(pred, opt.conf_thres, opt.iou_thres, classes=opt.classes, agnostic=opt.agnostic_nms)
        t3 = time_synchronized()

        # Process detections
        t4 = time.time()
        for i, det in enumerate(pred):
            if webcam:
                p, s,  im0, frame = path[i], '%g: ' % i, im0s[i].copy(), dataset.count
            else:
                p, im0, frame = path, im0s, getattr(dataset, 'frame', 0)
            s += '%gx%g ' % img.shape[2:] 
            imOrigin = im0.copy()
            p = Path(p)  # to Path
            save_path = str(save_dir / p.name) 

            os.makedirs(str(save_dir / 'labels'), exist_ok=True)
            txt_path = os.path.join(save_dir, 'labels', p.stem) if dataset.mode == 'image' else os.path.join(save_dir, 'labels', p.stem+f'_{frame}')

            # normalization gain whwh
            gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]
            if len(det):
                det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class
                    s += f"{n} {names[int(c)]}{'s' * (n > 1)} "

                # Write results
                for *xyxy, conf, cls in reversed(det):
                    if save_txt:  # Write to file
                        xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
                        line = (cls, *xywh, conf) if opt.save_conf else (cls, *xywh)
                        with open(os.path.join(txt_path+'.txt'), 'a') as f:
                            f.write(('%g ' * len(line)).rstrip() % line + '\n')
                    if save_img or view_img > -1:
                        label = f'{names[int(cls)]} {conf:.2f}'
                        textColor, bboxColor = BFC.getval(index=int(cls))
                        im0 = plot_one_box_with_return(xyxy, im0, label=label, txtColor=textColor, bboxColor=bboxColor, line_thickness=1)

            tmInf, tmNms = round(1E3 * (t2 - t1), 3), round(1E3 * (t3 - t2), 3)
            avgTime[0].append(tmInf)
            avgTime[1].append(tmNms)

            if view_img > -1:
                cv2.namedWindow(f'{dataset.mode} {path}', cv2.WINDOW_NORMAL)
                cv2.imshow(f'{dataset.mode} {path}', im0)
                if cv2.waitKey(view_img) == 27:
                    print(colorstr(f'User keyboard interupt, exiting...'))
                    exit()

            if save_img or opt.datacollection:
                if opt.datacollection:
                    im0 = imOrigin
                if dataset.mode == 'image':
                    cv2.imwrite(save_path, im0)
                    print(f"The image with the result is saved in: {save_path}")
                else: 
                    if vid_path != save_path:
                        vid_path = save_path
                        
                        if vid_cap: 
                            fps = vid_cap.get(cv2.CAP_PROP_FPS)
                            w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                            h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        else:
                            fps, w, h = 30, im0.shape[1], im0.shape[0]
                            save_path += '.mp4'                        
                        ffmpeg = FFMPEG_recorder(savePath=save_path,videoDimensions=(w,h),fps=fps)
                    ffmpeg.writeFrame(im0)
                    ffmpeg.writeSubtitle(title=s,fps=fps)
        print(f'{s}pre-proc {(time.time() - t4):0.3f}s, infer: {round(tmInf,3)}, nms: {round(tmNms,3)}')
    if save_txt or save_img:
        s = f"\n{len(list(save_dir.glob('labels/*.txt')))} labels saved to {save_dir / 'labels'}" if save_txt else ''
        print(f"Results saved to {save_dir}{s}")
    cv2.destroyAllWindows()
    if (save_img or opt.datacollection) and dataset.mode != 'image':
        ffmpeg.stopRecorder()
        ffmpeg.addSubtitle()
        
    print(f'Done. ({time.time() - t0:.3f}s), avgInference {round(sum(avgTime[0])/len(avgTime[0]),3)}ms, avgNMS {round(sum(avgTime[1])/len(avgTime[1]),3)}ms')

def detectTensorRT(tensorrtEngine,opt=None,save=''):
    source, weights, view_img, save_txt, imgsz, trace = opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size, not opt.no_trace
    save_img = not opt.nosave and not source.endswith('.txt') 
    webcam = is_stream_or_webcam(source=source)
    
    
    pred = TensorRT_Engine(TensorRT_EnginePath=tensorrtEngine, confThres=opt.conf_thres, iouThres=opt.iou_thres)
    imgsz = pred.imgsz
    stride = 32
    names = pred.names
    nc = pred.nc
    
    if webcam:
        dataset = LoadStreams(source, img_size=imgsz, stride=stride, auto= False)
    else:
        dataset = LoadImages(source, img_size=imgsz, stride=stride, auto= False)
    
    count = 0
    
    for path, img, im0s, vid_cap, s in dataset:
        t1 = time.time()
        img = pred.inference(im0s, end2end=True)
        if view_img > -1:
            cv2.namedWindow('TensortRT Engine', img)
            cv2.imshow('TensortRT Engine', cv2.WINDOW_NORMAL)
        if save_img:
            if dataset.mode == 'image':
                pa = f'{str(save)}/image{count}.jpg'
                count +=1
                ret = cv2.imwrite(pa,img)
                print(f'saved as {pa}, ret: {ret}, exit: {os.path.exists(pa)}')
            else:
                pass
    del pred, dataset
    if view_img > -1:
        cv2.destroyAllWindows()
        
        
imgs, img0s, dwdhs, ratios, avgTimeRate, avgFps = [], [],[], [], [], 0
pred, img_pred = None, None
thread2_img, thread2_img0s, thread2_dwdhs, thread2_ratios, thread2_avgTimeRate, thread2_avgFps = [], [],[], [], [], 0
stopThread = False

def inferWithDynamicBatch(enginePath,opt, save=''):
    source, weights, view_img, save_txt, imgsz, trace = opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size, not opt.no_trace
    save_img = not opt.nosave and not source.endswith('.txt') 
    webcam = is_stream_or_webcam(source)
    prefix = colorstr('ONNX: ')
    from models.yolo import ONNX_Engine
    model = None
    model = ONNX_Engine(ONNX_EnginePath=enginePath, prefix=prefix, confThres=opt.conf_thres)
    imgsz = model.imgsz
    if webcam:
        dataset = LoadStreams(source, img_size=imgsz, stride=model.stride, auto= model.rectangle)
    else:
        dataset = LoadImages(source, img_size=imgsz, stride=model.stride, auto= model.rectangle)
    BFC = BackgroundForegroundColors(names= model.names)
    model.batch_size = opt.batch_size if model.batch_size == 0 else model.batch_size
    print(f'{prefix}: {vars(model)}\n')
    t1_2_t2 = threading.Event()
    t2_2_t1 = threading.Event()
    t2_2_t3 = threading.Event()
    #t3_2_t2 = threading.Event()
    global thread2_avgFps
    thread2_avgFps = 1/model.warmup(10)
    print(f'speed: {thread2_avgFps}FPS')
       
    def Thread1():
        global imgs, img0s, dwdhs, ratios, seen, avgFps
        
        for path, img, im0s, vid_cap, s, ratio, dwdh in dataset:
            if stopThread:
                break
            if len(imgs) < model.batch_size:
                if webcam:
                    path = str(path[0])
                    for i in range(len(im0s)):
                        img0s.append(im0s[i])
                        imgs.append(model.preproc_for_infer(img[i]).copy())
                        dwdhs.append(dwdh[i])
                        ratios.append(ratio[i])
                else:
                    dwdhs = [dwdh] * model.batch_size
                    ratios = [ratio] * model.batch_size
                    img0s.append(im0s.copy())
                    imgs.append(model.preproc_for_infer(img).copy())
                    
            if len(imgs) >= model.batch_size:
                imgs = model.concat(imgs)        
                t1_2_t2.set()
                t2_2_t1.wait()
                t2_2_t1.clear()
                imgs, img0s, dwdhs, ratios, seen, avgFps = [], [],[], [], 0, 0
            print(f'debug: thread1 {s}, {im0s[0].shape}')
            
        imgs, img0s, dwdhs, ratios, seen, avgFps = [], [],[], [], 0, 0
        t1_2_t2.set()
        t2_2_t1.wait()
        t2_2_t1.clear()
        print(f'end: thread 1')
    
    def Thread2():
        global imgs, img0s, dwdhs, ratios
        global thread2_img, thread2_img0s, thread2_dwdhs, thread2_ratios, thread2_avgFps
        global pred, img_pred
        global stopThread
        seenn = 0
        avgTimeRate = []
        com = max(1, (64/model.batch_size))
        while True:
            t1_2_t2.wait()
            thread2_img, thread2_img0s, thread2_dwdhs, thread2_ratios = imgs, img0s, dwdhs, ratios
            t1_2_t2.clear()

            t2_2_t1.set()
            
            if len(thread2_img):
                t1 = time.time()
                pred, img_pred = model.infer(thread2_img)
                t2 = time.time() - t1
                avgTimeRate.append(t2)
                if seenn > com:
                    seenn = 0
                    thread2_avgFps = int(1/((sum(avgTimeRate) / len(avgTimeRate))/model.batch_size))
                    avgTimeRate = []
                seenn += 1
            else:
                print('------------------------------------------------------------------------------------------------')
                break
            t2_2_t3.set()
            #t3_2_t2.wait()
            #t3_2_t2.clear()
        print(f'end: thread2')
        
    def Thread3():
        global thread2_img, thread2_img0s, thread2_dwdhs, thread2_ratios, thread2_avgFps
        global stopThread
        seen = 0
        if not opt.nosave:
            ffmpeg = FFMPEG_recorder(f'{save_dir}/a.mp4', videoDimensions=(2560, 1440), fps=25)
        while True:
            t2_2_t3.wait()
            imgs, img0s, dwdhs, ratios, avgFps = thread2_img, thread2_img0s, thread2_dwdhs, thread2_ratios, thread2_avgFps
            global pred
            t2_2_t3.clear()
            img = model.end2end(pred[0], img0s, dwdhs, ratios, avgFps, BFC)
                        
            for im in img:
                cv2.namedWindow(f'Show {0}', cv2.WINDOW_NORMAL)
                cv2.imshow(f'Show {0}', im)
                avgFps = max(avgFps, 1)
                
                time.sleep((1/avgFps)+0.001)
                seen += 1
                if not opt.nosave:
                    ffmpeg.writeFrame(im)
                    
                if cv2.waitKey(opt.view_img) == 27 and opt.view_img > -1:
                    stopThread = True
                    print(f'end: thread3 {seen}')
                    break
            if stopThread:
                break
            #t3_2_t2.set()
    

    thread1 = threading.Thread(target= Thread1)
    thread2 = threading.Thread(target= Thread2)
    thread3 = threading.Thread(target= Thread3)
    print(f'starting')
    thread1.start()
    thread2.start()
    thread3.start()
    print('waiting')
    thread1.join()
    thread2.join()
    thread3.join()    
            
        
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', nargs='+', type=str,default=['./models/yolov7.pt'], help='model.pt path(s)')
    parser.add_argument('--source', type=str,default='inference/images/', help='source file/folder, 0 for webcam')
    parser.add_argument('--img-size', type=int, default=640,help='inference size (pixels)')
    parser.add_argument('--batch-size', type=int, default=1,help='inference batch (images)')
    parser.add_argument('--conf-thres', type=float,default=0.25, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float,default=0.45, help='IOU threshold for NMS')
    parser.add_argument('--device', default='cpu',help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--view-img',type=int, default=-1,help='display results')
    parser.add_argument('--save-txt', action='store_true',help='save results to *.txt')
    parser.add_argument('--save-conf', action='store_true',help='save confidences in --save-txt labels')
    parser.add_argument('--nosave', action='store_true',help='do not save images/videos')
    parser.add_argument('--datacollection',action='store_true', help='save image and labels')
    parser.add_argument('--classes', nargs='+', type=int,help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true',help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--update', action='store_true',help='update all models')
    parser.add_argument('--project', default='runs/detect', help='save results to project/name')
    parser.add_argument('--name', default='exp', help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--no-trace', action='store_true', help='don`t trace model')
    parser.add_argument('--no-check', action='store_true', help='don`t check requirements')
    
    opt = parser.parse_args()
    if not opt.no_check:
        check_requirements()
        check_git_status()
    save_dir = Path(increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok))  # increment run
    (save_dir / 'labels' if opt.save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir
    
    opt.weights = opt.weights if isinstance(opt.weights, list) else [opt.weights]
    frame_count_, fps_rate = 0, 0
    for _ in opt.weights:
        file_extention = os.path.splitext(_)[1]
        if file_extention in ['.pt']:
            with torch.no_grad():
                if opt.update:
                    detect(opt=opt)
                    strip_optimizer(f=_,halfModel=True)
                else:
                    detect(opt)
                    
        elif file_extention in ['.onnx']:
            inferWithDynamicBatch(_, opt, save_dir)
            # from models.yolo import ONNX_Engine
            # device = select_device(opt.device)[0]
            # webcam = is_stream_or_webcam(opt.source)
            # prefix = colorstr('ONNX_Engine')
            # model = ONNX_Engine(ONNX_EnginePath=_
            #                     ,confThres=opt.conf_thres, 
            #                     iouThres=opt.iou_thres,device=device,prefix= prefix)
            # imgsz = model.imgsz
            # if webcam:
            #     dataset = LoadStreams(opt.source, img_size=max(imgsz), auto= False if model.rectangle else True)
            # else:
            #     dataset = LoadImages(opt.source, img_size=max(imgsz), auto= False if model.rectangle else True)
                
            # names, vid_path = model.names, None
            
            # print(f'{prefix}: {vars(model)}\n')
            # BFC = BackgroundForegroundColors(names= names)
            # txtColorD, bboxColorD = BFC.getval(index=0)
            # half = device.type == 'cuda'
            # seen, hide_conf, hide_labels, avgSpeed, end2end = 0 , False, False, [], model.is_end3end
            # model.batch_size = opt.batch_size if model.batch_size == 0 else model.batch_size
            # imgs, img0s = [], []
            # for path, img, im0s, vid_cap, s, ratio, dwdh in dataset:
            #     model.warmup()
            #     t1 = time.time()
            #     pred, img = model.infer(img, end2end=end2end)                    
            #     t2 = time.time() - t1
            #     img_h, img_w = img.shape[2:]
                
            #     if not end2end:
            #         for i, det in enumerate(pred):
            #             seen += 1
            #             if webcam:
            #                 p, s,  im0, frame = path[i], '%g: ' % i, im0s[i].copy(), dataset.count
            #             else:
            #                 p, im0, frame = path, im0s.copy(), getattr(dataset, 'frame', 0)

            #             p = Path(p)
            #             save_path = str(save_dir / p.name)
            #             txt_path = str(save_dir / 'labels' / p.stem) + ('' if dataset.mode == 'image' else f'_{frame}')  # im.txt
            #             if frame_count_ == 0:
            #                 fps_rate = round(1000/(t2*1000),2)
            #                 avgSpeed.append(fps_rate)
            #                 fps_rate = sum(avgSpeed)/len(avgSpeed)
            #                 avgSpeed = []
            #                 frame_count_ += 1
            #             elif frame_count_ == 30:
            #                 frame_count_ = 0
            #             else:
            #                 frame_count_ += 1
            #             s += f'{img_h}x{img_w} '
            #             s += f'speed: {round(t2*1000,3)}ms '
            #             gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]  # normalization gain whwh
                        
            #             if len(det):
            #                 det[:, :4] = scale_coords((img_h, img_w), det[:, :4], im0.shape).round()

            #                 for c in det[:, 5].unique():
            #                     n = (det[:, 5] == c).sum()
            #                     s += f"{n} {names[int(c)]}{'s' * (n > 1)} "

            #                 for *xyxy, conf, cls in reversed(det):
            #                     if opt.save_txt:
            #                         xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
            #                         line = (cls, *xywh, conf) if opt.save_conf else (cls, *xywh)  # label format
            #                         with open(f'{txt_path}.txt', 'a') as f:
            #                             f.write(('%g ' * len(line)).rstrip() % line + '\n')

            #                     if not opt.nosave or opt.view_img:  # Add bbox to image
            #                         c = int(cls)
            #                         label = None if hide_labels else (names[c] if hide_conf else f'{names[c]} {conf:.2f}')
            #                         txtColor, bboxColor = BFC.getval(index=int(cls))
            #                         im0 = plot_one_box_with_return(xyxy, im0, label=label, txtColor=txtColor,
            #                                                     bboxColor=bboxColor, line_thickness=3,
            #                                                     frameinfo=[f'avgFPS: {fps_rate}',f'Total objects: {n}'])                                              
            #             else:
            #                 im0 = plot_one_box_with_return(None, im0, label=None, txtColor=txtColorD, bboxColor=bboxColorD,
            #                                             line_thickness=3, frameinfo=[f'avgFPS: {fps_rate}', f'Total objects: {0}'])                                
            #             print(f'{s}')
            #             if opt.view_img > -1:
            #                 cv2.namedWindow(f'{dataset.mode} {path}', cv2.WINDOW_NORMAL)
            #                 cv2.imshow(f'{dataset.mode} {path}', im0)
            #                 if cv2.waitKey(opt.view_img) == 27:
            #                     print(colorstr(f'User keyboard interupt, exiting...'))
            #                     exit()
            #             if not opt.nosave:
            #                 if dataset.mode == 'image':
            #                     pass
            #                 else:
            #                     if vid_path != save_path:
            #                         vid_path = save_path
            #                         if vid_cap:
            #                             fps = vid_cap.get(cv2.CAP_PROP_FPS)
            #                             w = vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            #                             h = vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            #                         else:
            #                             fps = 30
            #                             h, w = im0.shape[:2]
            #                         ffmpeg = FFMPEG_recorder(savePath=vid_path, videoDimensions=(int(w),int(h)), fps=fps)
                                    
            #                     ffmpeg.writeFrame(image=im0)
            #                     ffmpeg.writeSubtitle(title=s,fps=fps)
            #     else:
            #         pass
            #         if len(imgs) < model.batch_size:
            #             imgs.append(im0.copy())
            #             img0s.append(im0s.copy())
                    
            #         imgs = model.end2end(pred[0], img0s, dwdh, ratio, int(1/t2), BFC)
            #         if opt.view_imgs > -1:
            #             for im in imgs:
            #                 cv2.namedWindow(f'{dataset.mode} {path}', cv2.WINDOW_NORMAL)
            #                 cv2.imshow(f'{dataset.mode} {path}', im)
            #                 if cv2.waitKey(opt.view_img) == 27:
            #                     print(colorstr(f'User keyboard interupt, exiting...'))
            #                     exit()
            # if not opt.nosave and dataset.mode != 'image':          
            #     ffmpeg.stopRecorder()
            #     ffmpeg.addSubtitle()
            #     del ffmpeg
            # del model, dataset
            # cv2.destroyAllWindows()
                
        elif file_extention in ['.trt', '.engine']:
            detectTensorRT(_,opt, save=save_dir)
        elif file_extention in ['.tflite']:
            pass