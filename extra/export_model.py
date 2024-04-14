from typing import Tuple, Dict, List, Union
from tinygrad import Device, Tensor, Variable
from tinygrad.engine.jit import TinyJit, CompiledRunner 
from tinygrad.engine.realize import CustomOp
from tinygrad.nn.state import get_state_dict, get_parameters
from tinygrad.dtype import dtypes
import json

EXPORT_SUPPORTED_DEVICE = ["WEBGPU", "WEBGL", "CLANG", "CUDA", "GPU"]
web_utils = {
  "getTensorBuffer":
  """const getTensorBuffer = (safetensorBuffer, tensorMetadata) => {
    return safetensorBuffer.subarray(...tensorMetadata.data_offsets);
  }""",
  "getTensorMetadata": """const getTensorMetadata = (safetensorBuffer) => {
    const metadataLength = Number(new DataView(safetensorBuffer.buffer).getBigUint64(0, true));
    const metadata = JSON.parse(new TextDecoder("utf8").decode(safetensorBuffer.subarray(8, 8 + metadataLength)));
    return Object.fromEntries(Object.entries(metadata).filter(([k, v]) => k !== "__metadata__").map(([k, v]) => [k, {...v, data_offsets: v.data_offsets.map(x => 8 + metadataLength + x)}]));
  };"""
}

def compile_net(run: TinyJit, special_names:Dict[int,str], weight_names):
  functions, bufs, bufs_to_save, statements, bufnum = {}, {}, [], [], 0
  for ei in run.jit_cache:
    runner, cargs = ei.prg, []
    if not hasattr(runner, 'name'): continue # TODO: how should customop be handled?
    functions[runner.name] = runner.prg # NOTE: this assumes all with the same name are the same
    for i, buf in enumerate(ei.rawbufs):
      if buf is None: continue
      if (key := id(buf)) not in bufs:
        if key in special_names:
          bufs[key] = (special_names[key][0], buf.size*buf.dtype.itemsize, buf.dtype, buf, special_names[key][1])
        else:
          bufs[key] = (f"{weight_names[key]}" if key in weight_names else f"buf_{bufnum}", buf.size*buf.dtype.itemsize, buf.dtype, buf, True)
          bufnum += 0 if key in weight_names else 1
          if i > 0: bufs_to_save.append(key) # if first usage of a buffer is not an output, and it's not a special name
      cargs.append(bufs[key][0])
    for v in runner.vars:
      key = v.hash
      bufs[key] = (special_names[key][0], dtypes.int.itemsize, dtypes.int, None, special_names[key][1])
      cargs.append(bufs[key][0])
    statements.append((runner.name, cargs, runner.global_size, runner.local_size))

  return functions, statements, bufs, bufs_to_save

def jit_model(model, *args) -> Tuple[TinyJit,Dict[int,str]]:
  assert hasattr(model, "forward") or callable(model), "model needs a forward function"
  @TinyJit
  def run(*x):
    out = model.forward(*x) if hasattr(model, "forward") else model(*x)
    assert isinstance(out, tuple) or isinstance(out, list) or isinstance(out, Tensor), "model output must be a Tensor, tuple, or a list of Tensors for export"
    out = [out] if isinstance(out, Tensor) else out
    return [o.realize() for o in out]

  # twice to run the JIT
  for _ in range(2): output = run(*args)
  special_names = {}

  # hack to put the inputs back
  for (j,i),idx in run.input_replace.items():
    realized_input = args[idx].lazydata.base.realized
    run.jit_cache[j].rawbufs[i] = realized_input
    special_names[id(realized_input)] = (f"input{idx}", True)

  from tinygrad.engine.jit import get_jc_idxs_with_updatable_var_vals
  for j in get_jc_idxs_with_updatable_var_vals(run.jit_cache):
    for v in run.jit_cache[j].prg.vars: special_names[v.hash] = (f"input_{v.expr}", False)

  # TODO: fetch this from the jit in self.input_replace and self.ret (hint: use get_parameters on self.ret)
  for i, out in enumerate(output):
    special_names[id(out.lazydata.base.realized)] = (f"output{i}", out.dtype, True)
  return run, special_names

def fread_model_weights(fp: str, bufs, bufs_to_save):
  cprog = []
  cprog.append("#include <stdio.h>\n#include <stdlib.h>\n#include <assert.h>")
  cprog.append("void fread_net() {")
  cprog.append(f"  FILE *model_file = fopen(\"{fp}\", \"rb\");")
  cprog.append("  if (model_file == NULL) { printf(\"Error opening model file\\n\"); exit(1); }")
  cprog.append("  size_t s = 0;")

  for key in bufs_to_save:
    cprog.append(f"  s = fread({bufs[key][0]}_data, sizeof(float), {bufs[key][3].size}, model_file);")
    cprog.append(f"  assert(s == {bufs[key][3].size});")

  cprog.append("  fclose(model_file);")
  cprog.append("}")
  return cprog

def export_model_clang(functions, statements, bufs, bufs_to_save, net_inputs, net_outputs, net_keys, fread_weights=None) -> str:
  from tinygrad.runtime.ops_clang import CLANG_PROGRAM_HEADER
  cprog = [CLANG_PROGRAM_HEADER]

  for key in bufs_to_save:
    if fread_weights is not None and key in net_keys:
      cprog.append(f"float {bufs[key][0]}_data[{bufs[key][3].size}];")
    else:
      weight = ''.join(["\\x%02X"%x for x in bytes(bufs[key][3]._buf)])
      cprog.append(f"unsigned char {bufs[key][0]}_data[] = \"{weight}\";")

  if fread_weights is not None:
    cprog += fread_model_weights(fread_weights, bufs, list(filter(lambda x: x in net_keys, bufs_to_save)))

  for key,(name,len,dtype,_,_) in bufs.items():
    if key in bufs_to_save: cprog += [f"{dtype.name} *{name} = ({dtype.name} *){name}_data;"]
    else: cprog += [f"{dtype.name} {name}[{len}];"]

  cprog += list(functions.values())

  inputs = ", ".join([f"{dtype.name}{"*" if is_pointer else ""} {name}" for name,_,dtype,_,is_pointer in net_inputs])
  outputs = ", ".join([f"{dtype.name}{"*" if is_pointer else ""} {name}" for name,_,dtype,_,is_pointer in net_outputs])
  cprog += [f"void net({inputs}, {outputs}) {{"] + [f"  {name}({', '.join(args)});" for (name, args, _global_size, _local_size) in statements] + ["}"]
  return '\n'.join(cprog)

def export_model_webgl(functions, statements, bufs, weight_names) -> str:
  header = f"""
  function setupNet(gl, safetensor) {{
    function createShaderProgram(gl, code) {{
      const vertexShader = loadShader(gl, gl.VERTEX_SHADER, '#version 300 es\\nin vec2 in_position;in vec2 in_uv;out vec2 uv;void main(){{gl_Position=vec4(in_position,0.0,1.0);uv=in_uv;}}');
      const fragmentShader = loadShader(gl, gl.FRAGMENT_SHADER, code);
      const shaderProgram = gl.createProgram();
      gl.attachShader(shaderProgram, vertexShader);
      gl.attachShader(shaderProgram, fragmentShader);
      gl.linkProgram(shaderProgram);

      if (!gl.getProgramParameter(shaderProgram, gl.LINK_STATUS)) {{
        console.log(`Unable to initialize the shader program: ${{gl.getProgramInfoLog(shaderProgram)}}`);
        return null;
      }}

      return shaderProgram;
    }}

    function loadShader(gl, type, source) {{
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);

      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {{
        console.log(`An error occurred compiling the shaders: ${{gl.getShaderInfoLog(shader)}}`);
        gl.deleteShader(shader);
        return null;
      }}

      return shader;
    }}

    function setupVertexData(gl, program, vertices) {{
      let vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      let vertexBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
      const positionLocation = gl.getAttribLocation(program, 'in_position');
      const uvLocation = gl.getAttribLocation(program, 'in_uv');
      gl.enableVertexAttribArray(positionLocation);
      gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 4 * 4, 0);
      gl.enableVertexAttribArray(uvLocation);
      gl.vertexAttribPointer(uvLocation, 2, gl.FLOAT, false, 4 * 4, 2 * 4);
      gl.bindVertexArray(null);

      return vao;
    }}

    function runProgram(gl, kernelName, program, textures) {{
      let framebuffer = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, textures[0].tex, 0);
      gl.useProgram(program);
      gl.uniform1i(gl.getUniformLocation(program, "width"), textures[0].width);

      const vao = setupVertexData(gl, program, [-1, 1, 0, 1, -1, -1, 0, 0, 1, 1, 1, 1, 1, -1, 1, 0]);
      gl.bindVertexArray(vao);
      // Texture 0 is the framebuffer texture, so we skip that
      for (let i = 1; i < textures.length; i++) {{
        gl.activeTexture(gl.TEXTURE0 + i-1);
        gl.bindTexture(gl.TEXTURE_2D, textures[i].tex);
        gl.uniform1i(gl.getUniformLocation(program, 'data' + i), i-1);
      }}

      gl.viewport(0, 0, textures[0].width, textures[0].height);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);

      for (let i = 1; i < textures.length; i++) {{
        gl.activeTexture(gl.TEXTURE0 + i-1);
        gl.bindTexture(gl.TEXTURE_2D, null);
      }}

      console.log("Finished running: " + kernelName);
    }}

    function limitTextureDims(size, threshold) {{
      if (size <= threshold) {{ return [size, 1] }};

      for (let i = 2; i < threshold + 1; i++) {{
        if ((size % i == 0) && (Math.floor(size / i) <= threshold)) {{
          return [Math.floor(size / i), i];
        }}
      }}

      return [size, 1];
    }}

    function updateTextureData(gl, texture, data, isHalf) {{
      gl.bindTexture(gl.TEXTURE_2D, texture.tex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, texture.width, texture.height, gl.RED, (isHalf) ? gl.HALF_FLOAT : gl.FLOAT, data);
      gl.bindTexture(gl.TEXTURE_2D, null);
    }}

    function readTextureData(gl, texture) {{
      const framebuffer = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture.tex, 0);

      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {{
        throw new Error('Framebuffer not complete');
      }}

      let data = new Float32Array(texture.width * texture.height);
      gl.readPixels(0, 0, texture.width, texture.height, gl.RED, gl.FLOAT, data);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.deleteFramebuffer(framebuffer);

      return data;
    }}

    function createTexture(gl, size, isHalf, tensorBuffer) {{
      const texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, texture);
      const internalFormat = gl.RGBA;
      const texSize = limitTextureDims(size, gl.getParameter(gl.MAX_TEXTURE_SIZE));
      let weights;

      if (tensorBuffer != null) {{
        if (!isHalf)
          weights = new Float32Array(tensorBuffer.buffer, tensorBuffer.byteOffset, tensorBuffer.byteLength / Float32Array.BYTES_PER_ELEMENT);
        else
          weights = new Uint16Array(tensorBuffer.buffer, tensorBuffer.byteOffset, tensorBuffer.byteLength / Uint16Array.BYTES_PER_ELEMENT);
      }} else {{
        if (!isHalf)
          weights = new Float32Array(size).fill(0.0);
        else
          weights = new Uint16Array(size).fill(0.0);
      }}

      if (size != weights.length)
        console.log("Weights length: " + weights.length + ", texsize: " + texSize[0]*texSize[1]);

      gl.texImage2D(gl.TEXTURE_2D, 0, (isHalf) ? gl.R16F : gl.R32F, texSize[0], texSize[1], 0, gl.RED, (isHalf) ? gl.HALF_FLOAT : gl.FLOAT, weights);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.bindTexture(gl.TEXTURE_2D, null);
      return {{ tex: texture, width: texSize[0], height: texSize[1] }};
    }}

    {web_utils["getTensorBuffer"]}
    {web_utils["getTensorMetadata"]}

    const metadata = getTensorMetadata(safetensor);
  """

  textures = '\n    '.join([f"const {name} = " + (f"createTexture(gl, {size/(2 if dtype == dtypes.half else 4)}, {'true' if dtype == dtypes.half else 'false'});" if _key not in weight_names else f"createTexture(gl, {size/(2 if dtype == dtypes.half else 4)}, {'true' if dtype == dtypes.half else 'false'}, getTensorBuffer(safetensor, metadata['{weight_names[_key]}']))") + ";"  for _key,(name,size_,dtype,_,_) in bufs.items()])
  kernels = '\n\n'.join([f"const {key} = `{code.replace(key, 'main').replace('version 330', 'version 300 es')}`;" for key, code in functions.items()])
  kernel_names = ', '.join([name for (name, _args, _global_size, _local_size) in statements])
  kernel_calls = '\n        '.join([f"runProgram(gl, '{name}', programs[{i}], [{', '.join(args)}]);" for i, (name, args, _global_size, _local_size) in enumerate(statements) ])
  copy_inputs = "\n".join([f'updateTextureData(gl, {name}, _{name}, {"true" if dtype == dtypes.half else "false"});' for name,size,_,dtype,_,_ in bufs.values() if "input" in name])
  entry_point = f"""
    return function({",".join([f"_{name}" for name,size,dtype,_,_ in bufs.values() if "input" in name])}) {{
      const ext = gl.getExtension('EXT_color_buffer_float');
      {copy_inputs}
      {kernel_calls}

      return readTextureData(gl, output0);
    }}
  """
  programs = f"let programs = [{kernel_names}].map((code) => createShaderProgram(gl, code));"
  return f"{header}\n{kernels}\n{textures}\n{programs}\n{entry_point}}}"

def export_model_webgpu(functions, statements, bufs, weight_names, input_names, output_names) -> Tuple[str,int,int]:
  kernel_code = '\n\n'.join([f"const {key} = `{code.replace(key, 'main')}`;" for key, code in functions.items()])
  kernel_names = ', '.join([name for (name, _args, _global_size, _local_size) in statements])
  kernel_calls = '\n        '.join([f"addComputePass(device, commandEncoder, piplines[{i}], [{', '.join(args)}], {global_size});" for i, (_name, args, global_size, _local_size) in enumerate(statements) ])
  _bufs =  '\n    '.join([f"const {name} = " + (f"createEmptyBuf(device, {size});" if _key not in weight_names else f"createWeightBuf(device, {size}, getTensorBuffer(safetensor, metadata['{weight_names[_key]}']))") + ";"  for _key,(name,size,dtype,_,_) in bufs.items()])
  gpu_write_bufs =  '\n    '.join([f"const gpuWriteBuffer{i} = device.createBuffer({{size:{input_name}.size, usage: GPUBufferUsage.COPY_SRC | GPUBufferUsage.MAP_WRITE }});" for i,input_name in enumerate(input_names)])
  input_writers = '\n    '.join([f"await gpuWriteBuffer{i}.mapAsync(GPUMapMode.WRITE);\n        new Float32Array(gpuWriteBuffer{i}.getMappedRange()).set(" + f'_{inp_name});' + f"\n        gpuWriteBuffer{i}.unmap();\n        commandEncoder.copyBufferToBuffer(gpuWriteBuffer{i}, 0, {inp_name}, 0, gpuWriteBuffer{i}.size);"  for i,inp_name in enumerate(input_names)])
  gpu_read_bufs = '\n    '.join([f"const gpuReadBuffer{i} = device.createBuffer({{size:{output_name}.size, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ }});" for i,output_name in enumerate(output_names)])
  outbuf_copies = '\n        '.join([f"commandEncoder.copyBufferToBuffer({output_name}, 0, gpuReadBuffer{i}, 0, output{i}.size);" for i,output_name in enumerate(output_names)])
  output_readers = '\n        '.join([f"await gpuReadBuffer{i}.mapAsync(GPUMapMode.READ);\n        const resultBuffer{i} = new Float32Array(gpuReadBuffer{i}.size);\n        resultBuffer{i}.set(new Float32Array(gpuReadBuffer{i}.getMappedRange()));\n        gpuReadBuffer{i}.unmap();" for i in range(len(output_names))])
  output_return = '[{}]'.format(",".join([f'resultBuffer{i}' for i in range(len(output_names))]))
  return f"""
{web_utils["getTensorBuffer"]}

{web_utils["getTensorMetadata"]}

const createEmptyBuf = (device, size) => {{
    return device.createBuffer({{size, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC | GPUBufferUsage.COPY_DST }});
}};

const createWeightBuf = (device, size, data) => {{
  const buf = device.createBuffer({{ mappedAtCreation: true, size, usage: GPUBufferUsage.STORAGE }});
  new Uint8Array(buf.getMappedRange()).set(data);
  buf.unmap();
  return buf;
}};

const addComputePass = (device, commandEncoder, pipeline, bufs, workgroup) => {{
  const bindGroup = device.createBindGroup({{layout: pipeline.getBindGroupLayout(0), entries: bufs.map((buffer, index) => ({{ binding: index, resource: {{ buffer }} }}))}});
  const passEncoder = commandEncoder.beginComputePass();
  passEncoder.setPipeline(pipeline);
  passEncoder.setBindGroup(0, bindGroup);
  passEncoder.dispatchWorkgroups(...workgroup);
  passEncoder.end();
}};

{kernel_code}

const setupNet = async (device, safetensor) => {{
    const metadata = getTensorMetadata(safetensor);

    {_bufs}

    {gpu_write_bufs}

    {gpu_read_bufs}

    const kernels = [{kernel_names}];
    const piplines = await Promise.all(kernels.map(name => device.createComputePipelineAsync({{layout: "auto", compute: {{ module: device.createShaderModule({{ code: name }}), entryPoint: "main" }}}})));

    return async ({",".join([f"_{input_name}" for input_name in input_names])}) => {{
        const commandEncoder = device.createCommandEncoder();
        {input_writers}
        {kernel_calls}
        {outbuf_copies}
        const gpuCommands = commandEncoder.finish();
        device.queue.submit([gpuCommands]);

        {output_readers}
        return {output_return};
    }}
}}
  """ + f"\n\nconst loadNet = async (device) => {{ return await fetch('net.safetensors').then(x => x.arrayBuffer()).then(x => setupNet(device, new Uint8Array(x))); }}"

def export_model(model, target:str, *inputs, fread_weights=None):
  assert Device.DEFAULT in EXPORT_SUPPORTED_DEVICE, "only WEBGPU, WEBGL, CLANG, CUDA, GPU, METAL are supported"
  run, special_names = jit_model(model, *inputs)
  state = get_state_dict(model)
  weight_names = {id(x.lazydata.realized): name.replace(".", "_") for name, x in state.items()}
  functions, statements, bufs, bufs_to_save = compile_net(run, special_names, weight_names)
  net_inputs = list(filter(lambda x: "input" in x[0], bufs.values()))
  net_outputs = list(filter(lambda x: "output" in x[0], bufs.values()))
  prg = ""
  if target == "clang":
    prg = export_model_clang(functions, statements, bufs, bufs_to_save, net_inputs, net_outputs, weight_names, fread_weights)
  elif target == "webgpu":
    prg = export_model_webgpu(functions, statements, bufs, weight_names)
  elif target == "webgl":
    prg = export_model_webgl(functions, statements, bufs, weight_names, [name for name,_,_,_,_ in net_inputs], [name for name,_,_,_,_ in net_outputs.values()])
  else:
    prg = json.dumps({
      "backend": Device.DEFAULT,
      "inputs": [{
        "size": bufs[name][0],
        "dtype": bufs[name][1].name
      } for name,_,_,_,_ in net_inputs.values()],
      "outputs": [{
        "size": bufs[name][0],
        "dtype": bufs[name][1].name
      } for name,_,_,_,_ in net_outputs],
      "functions": functions,
      "statements": [{
        "kernel": kernel,
        "args": args,
        "global_size": global_size,
        "local_size": local_size
      } for (kernel, args, global_size, local_size) in statements],
      "buffers": {
        name: {
          "size": size,
          "dtype": dtype.name,
          "id": weight_names[_key] if _key in weight_names else ""
        } for _key,(name,size,_,dtype,_,_) in bufs.items() if name not in ["input", "outputs"]
      }
    })

  return prg, net_inputs, net_outputs, state
